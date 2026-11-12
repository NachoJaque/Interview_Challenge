from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-secret-change-me-before-deploying"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://krunchbox:krunchbox@127.0.0.1:5432/krunchbox"

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    cors_origins: list[str] = ["http://localhost:5173"]
    # Path prefix a reverse proxy strips before forwarding (e.g. "/api"), so
    # the interactive docs generate correct URLs when served behind it.
    root_path: str = ""

    # Purchase rules
    max_tickets_per_purchase: int = 10
    reservation_ttl_minutes: int = 10
    # How often the background task releases expired reservations (0 disables it).
    reservation_sweep_seconds: int = 10

    # Refund rules (see README "Refund rules")
    full_refund_min_hours: int = 7 * 24  # this long or more before the event: 100%
    partial_refund_min_hours: int = 48  # this long or more before the event: partial_refund_percentage
    partial_refund_percentage: int = 50

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
