"""Tests run against a real PostgreSQL database: the behaviour under test
(row locks, conditional UPDATEs, CHECK constraints, partial unique indexes)
does not exist in SQLite.

Start one with `docker compose up -d db`. The `krunchbox_test` database is
created automatically and is separate from the development data.
"""

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://krunchbox:krunchbox@127.0.0.1:5432/krunchbox_test"
)
# Must be set before the app is imported, since the engine is built at import time.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["RESERVATION_SWEEP_SECONDS"] = "0"  # tests trigger the sweeper explicitly
os.environ["ENVIRONMENT"] = "test"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Event, TicketType, User  # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402

# Hashing is deliberately slow; hash the shared test password once.
TEST_PASSWORD = "test-password-123"
_TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)


def _create_test_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin_engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin_engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    _create_test_database()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(_schema: None) -> Iterator[None]:
    yield
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_user(db: Session) -> Callable[..., User]:
    counter = 0

    def _make(email: str | None = None, is_admin: bool = False) -> User:
        nonlocal counter
        counter += 1
        user = User(
            email=email or f"user{counter}@example.com",
            full_name=f"User {counter}",
            hashed_password=_TEST_PASSWORD_HASH,
            is_admin=is_admin,
        )
        db.add(user)
        db.commit()
        return user

    return _make


@pytest.fixture
def user(make_user: Callable[..., User]) -> User:
    return make_user()


@pytest.fixture
def make_event(db: Session) -> Callable[..., Event]:
    def _make(
        starts_in: timedelta = timedelta(days=30),
        general_capacity: int = 100,
        vip_capacity: int = 10,
        vip_refundable: bool = True,
    ) -> Event:
        event = Event(
            name="Test Event",
            venue="Test Venue",
            starts_at=datetime.now(UTC) + starts_in,
            ticket_types=[
                TicketType(name="General", price=Decimal("50.00"), capacity=general_capacity),
                TicketType(
                    name="VIP", price=Decimal("150.00"), capacity=vip_capacity, is_refundable=vip_refundable
                ),
            ],
        )
        db.add(event)
        db.commit()
        return event

    return _make


def auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def ticket_type(event: Event, name: str) -> TicketType:
    return next(tt for tt in event.ticket_types if tt.name == name)


def counters(ticket_type_id: int) -> tuple[int, int]:
    """(held, sold) read with a fresh session, i.e. what is really committed."""
    with SessionLocal() as session:
        tt = session.get(TicketType, ticket_type_id)
        return tt.held, tt.sold
