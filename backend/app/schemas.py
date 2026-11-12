from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import PurchaseStatus


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected, so a client can
    never smuggle in attributes such as `is_admin` or `unit_price`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Auth -----------------------------------------------------------------


class UserCreate(RequestModel):
    email: EmailStr = Field(max_length=255)
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def fits_bcrypt(cls, v: str) -> str:
        if len(v.encode()) > 72:  # bcrypt only hashes the first 72 bytes
            raise ValueError("password is too long")
        return v


class UserRead(ORMModel):
    id: int
    email: str
    full_name: str
    is_admin: bool


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# --- Events ----------------------------------------------------------------


class TicketTypeCreate(RequestModel):
    name: str = Field(min_length=1, max_length=50)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    capacity: int = Field(ge=0, le=1_000_000)
    is_refundable: bool = True


class TicketTypeRead(ORMModel):
    id: int
    name: str
    price: Decimal
    capacity: int
    held: int
    sold: int
    available: int
    is_refundable: bool


class EventCreate(RequestModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    venue: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ticket_types: list[TicketTypeCreate] = Field(min_length=1, max_length=20)

    @field_validator("starts_at")
    @classmethod
    def require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("starts_at must include a timezone offset")
        return v


class EventRead(ORMModel):
    id: int
    name: str
    description: str | None
    venue: str
    starts_at: datetime
    ticket_types: list[TicketTypeRead]


# --- Purchases -------------------------------------------------------------


class PurchaseItemRequest(RequestModel):
    ticket_type_id: int = Field(gt=0, le=2_147_483_647)
    quantity: int = Field(gt=0, le=100)


class PurchaseRequest(RequestModel):
    items: list[PurchaseItemRequest] = Field(min_length=1, max_length=20)


class PurchaseItemRead(BaseModel):
    id: int
    ticket_type_id: int
    ticket_type_name: str
    quantity: int
    refunded_quantity: int
    refundable_quantity: int
    unit_price: Decimal
    is_refundable: bool


class RefundItemRead(ORMModel):
    purchase_item_id: int
    quantity: int
    amount: Decimal


class RefundRead(ORMModel):
    id: int
    amount: Decimal
    refund_percentage: int
    reason: str | None
    created_at: datetime
    items: list[RefundItemRead]


class RefundPolicyRead(ORMModel):
    allowed: bool
    percentage: int
    reason: str


class PurchaseRead(BaseModel):
    id: int
    event_id: int
    event_name: str
    event_starts_at: datetime
    status: PurchaseStatus
    total_amount: Decimal
    refunded_amount: Decimal
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None
    items: list[PurchaseItemRead]
    refunds: list[RefundRead]
    # What a refund requested right now would get.
    refund_policy: RefundPolicyRead


# --- Refunds ---------------------------------------------------------------


class RefundItemRequest(RequestModel):
    purchase_item_id: int = Field(gt=0, le=2_147_483_647)
    quantity: int = Field(gt=0, le=100)


class RefundRequest(RequestModel):
    items: list[RefundItemRequest] = Field(min_length=1, max_length=20)
    reason: str | None = Field(default=None, max_length=500)
