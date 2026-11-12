import enum
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

Money = Numeric(10, 2)


def utcnow() -> datetime:
    return datetime.now(UTC)


class PurchaseStatus(enum.StrEnum):
    PENDING = "pending"  # tickets are held, waiting for confirmation
    COMPLETED = "completed"
    CANCELLED = "cancelled"  # reservation released by the user
    EXPIRED = "expired"  # reservation released because it was not confirmed in time
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    purchases: Mapped[list["Purchase"]] = relationship(back_populates="user")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    venue: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket_types: Mapped[list["TicketType"]] = relationship(
        back_populates="event", order_by="TicketType.price", cascade="all, delete-orphan"
    )


class TicketType(Base):
    """A sellable category of tickets for one event (e.g. General, VIP).

    Ticket types are rows, not an enum: adding a new category ("Platinum",
    "Early Bird", ...) is an insert, with no code or schema change.

    `held` (pending reservations) and `sold` (confirmed purchases) are the
    single source of truth for capacity. They are only ever modified through
    atomic UPDATEs (see services/purchases.py), and the CHECK constraint makes
    overselling impossible even if application code is wrong.
    """

    __tablename__ = "ticket_types"
    __table_args__ = (
        # Case-insensitive: "VIP" and "vip" are the same category.
        Index("uq_ticket_types_event_name", "event_id", text("lower(name)"), unique=True),
        CheckConstraint("capacity >= 0", name="ck_ticket_types_capacity_non_negative"),
        CheckConstraint(
            "held >= 0 AND sold >= 0 AND held + sold <= capacity",
            name="ck_ticket_types_within_capacity",
        ),
        CheckConstraint("price >= 0", name="ck_ticket_types_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(50))
    price: Mapped[Decimal] = mapped_column(Money)
    capacity: Mapped[int] = mapped_column(Integer)
    held: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sold: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_refundable: Mapped[bool] = mapped_column(Boolean, default=True)

    event: Mapped[Event] = relationship(back_populates="ticket_types")

    @property
    def available(self) -> int:
        return self.capacity - self.held - self.sold


class Purchase(Base):
    """A reservation and the purchase it becomes are the same row.

    pending -> completed -> partially_refunded -> refunded
    pending -> cancelled | expired
    """

    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_purchases_user_idempotency_key"),
        # A user can hold at most one pending reservation per event.
        Index(
            "uq_purchases_one_pending_per_user_event",
            "user_id",
            "event_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # Lets the expiry sweeper find stale reservations cheaply.
        Index("ix_purchases_pending_expires_at", "expires_at", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    # Stored as VARCHAR + CHECK rather than a native enum, so adding a status
    # is a plain migration instead of ALTER TYPE.
    status: Mapped[PurchaseStatus] = mapped_column(
        Enum(
            PurchaseStatus,
            name="purchase_status",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PurchaseStatus.PENDING,
    )
    total_amount: Mapped[Decimal] = mapped_column(Money)
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="purchases")
    event: Mapped[Event] = relationship()
    items: Mapped[list["PurchaseItem"]] = relationship(
        back_populates="purchase", order_by="PurchaseItem.id", cascade="all, delete-orphan"
    )
    refunds: Mapped[list["Refund"]] = relationship(
        back_populates="purchase", order_by="Refund.id", cascade="all, delete-orphan"
    )

    @property
    def refunded_amount(self) -> Decimal:
        return sum((r.amount for r in self.refunds), Decimal("0.00"))


class PurchaseItem(Base):
    """One ticket type within a purchase. `unit_price` is a snapshot of the
    price at reservation time so later price changes never affect refunds."""

    __tablename__ = "purchase_items"
    __table_args__ = (
        UniqueConstraint("purchase_id", "ticket_type_id", name="uq_purchase_items_purchase_ticket_type"),
        CheckConstraint("quantity > 0", name="ck_purchase_items_quantity_positive"),
        CheckConstraint(
            "refunded_quantity >= 0 AND refunded_quantity <= quantity",
            name="ck_purchase_items_refunded_within_quantity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id", ondelete="CASCADE"), index=True)
    ticket_type_id: Mapped[int] = mapped_column(ForeignKey("ticket_types.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    refunded_quantity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    purchase: Mapped[Purchase] = relationship(back_populates="items")
    ticket_type: Mapped[TicketType] = relationship()

    @property
    def refundable_quantity(self) -> int:
        return self.quantity - self.refunded_quantity


class Refund(Base):
    """Immutable record of one refund operation (a purchase can have several)."""

    __tablename__ = "refunds"
    __table_args__ = (
        UniqueConstraint("purchase_id", "idempotency_key", name="uq_refunds_purchase_idempotency_key"),
        CheckConstraint("amount >= 0", name="ck_refunds_amount_non_negative"),
        CheckConstraint("refund_percentage > 0 AND refund_percentage <= 100", name="ck_refunds_percentage_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    # The policy tier applied when the refund was made (policy can change later).
    refund_percentage: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    purchase: Mapped[Purchase] = relationship(back_populates="refunds")
    items: Mapped[list["RefundItem"]] = relationship(
        back_populates="refund", order_by="RefundItem.id", cascade="all, delete-orphan"
    )


class RefundItem(Base):
    __tablename__ = "refund_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_refund_items_quantity_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    refund_id: Mapped[int] = mapped_column(ForeignKey("refunds.id", ondelete="CASCADE"), index=True)
    purchase_item_id: Mapped[int] = mapped_column(ForeignKey("purchase_items.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Money)

    refund: Mapped[Refund] = relationship(back_populates="items")
    purchase_item: Mapped[PurchaseItem] = relationship()
