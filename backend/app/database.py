import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout_seconds,
    connect_args={
        "options": (
            f"-c statement_timeout={_settings.db_statement_timeout_ms} "
            f"-c lock_timeout={_settings.db_lock_timeout_ms} "
            f"-c idle_in_transaction_session_timeout={_settings.db_idle_in_transaction_timeout_ms}"
        )
    },
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Sessions are closed on these threads, never on the request worker threads.
_session_closers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db-close")


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[Session]:
    """One session per request.

    Deliberately an async dependency: a sync one is cleaned up on a worker
    thread, so a finished request would have to queue for a thread just to
    give its connection back. Closing on a small pool of its own returns the
    connection the moment the response is ready. (admission.py is what keeps
    the number of requests holding a connection within the pool.)
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        await asyncio.get_running_loop().run_in_executor(_session_closers, session.close)
