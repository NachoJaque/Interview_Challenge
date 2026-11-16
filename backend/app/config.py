from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-secret-change-me-before-deploying"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://krunchbox:krunchbox@127.0.0.1:5432/krunchbox"

    # Connections this process may hold, which is also how many requests are
    # let into the application at once and how many worker threads it has (see
    # admission.py and main.py). A request in progress is thereby guaranteed a
    # connection and a thread; the rest queue before using either.
    db_pool_size: int = 40
    # Headroom for work outside the request queue (the sweeper, health checks).
    db_max_overflow: int = 5
    # How long a request may queue for a place before it is answered 503.
    request_queue_timeout_seconds: float = 10
    # How long to wait for a free connection before answering 503.
    db_pool_timeout_seconds: float = 5
    # Server-side limits, so one stuck query or lock cannot stall every buyer.
    db_statement_timeout_ms: int = 30_000
    db_lock_timeout_ms: int = 5_000
    db_idle_in_transaction_timeout_ms: int = 60_000

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # Browser origins allowed to call the API directly. Empty by default: the
    # web app reaches the API through its own origin (nginx or the Vite proxy).
    cors_origins: list[str] = []
    # Path prefix a reverse proxy strips before forwarding (e.g. "/api"), so
    # the interactive docs generate correct URLs when served behind it.
    root_path: str = ""

    # Purchase rules
    max_tickets_per_purchase: int = 10
    # Tickets one customer may hold for one event across all their purchases.
    max_tickets_per_user_per_event: int = 20
    # Reservations a customer may let expire or cancel for one event within the
    # window before new ones are refused (stops holding tickets without buying).
    max_abandoned_reservations: int = 3
    abandoned_reservation_window_minutes: int = 60
    reservation_ttl_minutes: int = 10
    # How often the background task releases expired reservations (0 disables it).
    reservation_sweep_seconds: int = 10
    # Expired reservations one purchase request may release on its way in. The
    # sweeper releases the rest, so a request never pays for a whole backlog.
    reservation_lazy_release_limit: int = 5
    # How long the public event data may be served from memory (0 disables it).
    events_cache_seconds: float = 1.0

    # Refund rules (see README "Refund rules")
    full_refund_min_hours: int = 7 * 24  # this long or more before the event: 100%
    partial_refund_min_hours: int = 48  # this long or more before the event: partial_refund_percentage
    partial_refund_percentage: int = 50

    # Whether the seed script also loads the demo customer and demo events.
    seed_demo_data: bool = True

    # Accounts created by the seed script (python -m app.seed)
    admin_email: str | None = None
    admin_password: str | None = None
    demo_email: str | None = None
    demo_password: str | None = None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def require_real_secret_in_production(self) -> Self:
        if self.is_production and (self.jwt_secret == DEFAULT_JWT_SECRET or len(self.jwt_secret) < 32):
            raise ValueError("JWT_SECRET must be set to a random value of at least 32 characters in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
