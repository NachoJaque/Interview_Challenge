import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import SessionLocal
from app.exceptions import DomainError, domain_error_handler
from app.routers import auth, events, purchases
from app.services.purchases import release_expired_purchases

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
    version="1.0.0",
    lifespan=lifespan,
    root_path=settings.root_path,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # API responses carry account data: never cache them.
    response.headers.setdefault("Cache-Control", "no-store")
    if settings.is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.add_exception_handler(DomainError, domain_error_handler)

app.include_router(auth.router)
app.include_router(events.router)
app.include_router(purchases.router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
