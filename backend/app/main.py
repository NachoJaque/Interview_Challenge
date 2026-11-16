import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from anyio import to_thread
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.admission import AdmissionMiddleware
from app.audit import request_id_var
from app.config import get_settings
from app.database import SessionLocal
from app.deps import DbSession
from app.exceptions import register_error_handlers
from app.routers import auth, events, purchases
from app.schemas import ErrorResponse
from app.services.purchases import release_expired_purchases

# uvicorn only configures its own loggers; without this the application's
# INFO lines (the audit trail, the sweeper) would be dropped.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

logger = logging.getLogger(__name__)
settings = get_settings()


def _sweep_expired_reservations() -> int:
    with SessionLocal() as db:
        return release_expired_purchases(db)


async def _reservation_sweeper(interval_seconds: int) -> None:
    """Periodically return the tickets of unconfirmed reservations to the pool."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            released = await run_in_threadpool(_sweep_expired_reservations)
            if released:
                logger.info("Released %d expired reservation(s)", released)
        except Exception:
            logger.exception("Reservation sweep failed; will retry")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # One thread per request in progress is enough: a request uses one at a time.
    to_thread.current_default_thread_limiter().total_tokens = settings.db_pool_size + settings.db_max_overflow

    sweeper = None
    if settings.reservation_sweep_seconds > 0:
        sweeper = asyncio.create_task(_reservation_sweeper(settings.reservation_sweep_seconds))
    yield
    if sweeper is not None:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


app = FastAPI(
    title="Krunchbox Ticketing API",
    description="Sell and refund event tickets without ever overselling.",
    version="1.2.0",
    lifespan=lifespan,
    redoc_url=None,
    # Shown in the interactive docs for every endpoint; see exceptions.py.
    responses={
        422: {"model": ErrorResponse, "description": "A business rule was broken, or the input is invalid."},
        503: {"model": ErrorResponse, "description": "Over capacity. Retry after the `Retry-After` seconds."},
    },
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
    )


# Added first, so it sits innermost: a request waiting for a place has
# already been tagged and will carry the security headers either way.
app.add_middleware(
    AdmissionMiddleware,
    limit=settings.db_pool_size,
    max_wait_seconds=settings.request_queue_timeout_seconds,
    exempt_paths=frozenset({"/health"}),
)

Handler = Callable[[Request], Awaitable[Response]]


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Handler) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # API responses carry account data: never cache them.
    response.headers.setdefault("Cache-Control", "no-store")
    if settings.is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.middleware("http")
async def tag_request(request: Request, call_next: Handler) -> Response:
    """Give the request an id (always generated here, never taken from the
    client), visible to the audit log while it runs and returned to the caller."""
    request_id = uuid.uuid4().hex
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


register_error_handlers(app)

app.include_router(auth.router)
app.include_router(events.router)
app.include_router(purchases.router)


@app.get("/health", tags=["health"])
def health(db: DbSession, response: Response) -> dict[str, str]:
    """Ready only if the database answers, so a load balancer or Docker stops
    sending traffic to an instance that cannot serve it."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Health check could not reach the database")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ok"}
