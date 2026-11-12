"""Purchase lifecycle: reserve -> confirm, or cancel / expire.

Locking rules shared by every function here (and by services/refunds.py), so
that no two transactions can deadlock:

1. Lock the purchase row first (SELECT ... FOR UPDATE), then ticket types.
2. Touch ticket_type rows in ascending id order.
3. Hold locks for one purchase per transaction.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import Settings
from app.exceptions import (
    ConflictError,
    InsufficientCapacityError,
    InvalidPurchaseError,
    InvalidPurchaseStateError,
    NotFoundError,
    PendingReservationExistsError,
    ReservationExpiredError,
    SalesClosedError,
    TicketLimitExceededError,
    TooManyReservationsError,
)
from app.models import Event, Purchase, PurchaseItem, PurchaseStatus, TicketType, User
from app.schemas import PurchaseRequest

ONE_PENDING_CONSTRAINT = "uq_purchases_one_pending_per_user_event"


def _find_by_idempotency_key(db: Session, user_id: int, key: str) -> Purchase | None:
    return db.scalar(select(Purchase).where(Purchase.user_id == user_id, Purchase.idempotency_key == key))


def lock_purchase(db: Session, user: User, purchase_id: int) -> Purchase:
    """Load one of the user's purchases and lock it for the rest of the transaction.

    Filtering by user_id means someone else's purchase is indistinguishable
    from a missing one (404), so ids cannot be probed.
    """
    purchase = db.scalar(
        select(Purchase)
        .where(Purchase.id == purchase_id, Purchase.user_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if purchase is None:
        raise NotFoundError(f"Purchase {purchase_id} not found.")
    return purchase


def _release_hold(db: Session, purchase: Purchase, new_status: PurchaseStatus) -> None:
    """Return a pending purchase's held tickets to the pool. Caller holds the purchase lock."""
    for item in sorted(purchase.items, key=lambda i: i.ticket_type_id):
        db.execute(
            update(TicketType).where(TicketType.id == item.ticket_type_id).values(held=TicketType.held - item.quantity)
        )
    purchase.status = new_status


def release_expired_purchases(db: Session, now: datetime | None = None, event_id: int | None = None) -> int:
    """Expire pending reservations whose hold has run out. Returns how many were released.

    Safe to run from several workers at once: each purchase is released in its
    own transaction, and SKIP LOCKED skips rows another transaction is busy
    with (e.g. a confirmation in progress, which then decides the outcome).
    """
    now = now or datetime.now(UTC)
    expired = (Purchase.status == PurchaseStatus.PENDING) & (Purchase.expires_at <= now)

    candidates = select(Purchase.id).where(expired)
    if event_id is not None:
        candidates = candidates.where(Purchase.event_id == event_id)
    purchase_ids = db.scalars(candidates).all()
    db.rollback()  # end the read transaction; each release below is its own

    released = 0
    for purchase_id in purchase_ids:
        purchase = db.scalar(
            select(Purchase)
            .where(Purchase.id == purchase_id, expired)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if purchase is None:
            db.rollback()
            continue
        _release_hold(db, purchase, PurchaseStatus.EXPIRED)
        db.commit()
        audit("reservation_expired", purchase_id=purchase.id, user_id=purchase.user_id)
        released += 1
    return released


def _check_customer_limits(
    db: Session, user: User, event_id: int, requested: int, settings: Settings, now: datetime
) -> None:
    """Per-customer rules for one event. The caller has already established
    that the user has no pending reservation for it; the partial unique index
    then keeps concurrent requests from the same user from both passing."""
    window_start = now - timedelta(minutes=settings.abandoned_reservation_window_minutes)
    abandoned = db.scalar(  # count() and coalesce() never return NULL
        select(func.count())
        .select_from(Purchase)
        .where(
            Purchase.user_id == user.id,
            Purchase.event_id == event_id,
            Purchase.status.in_((PurchaseStatus.CANCELLED, PurchaseStatus.EXPIRED)),
            Purchase.created_at >= window_start,
        )
    )
    if (abandoned or 0) >= settings.max_abandoned_reservations:
        raise TooManyReservationsError(
            "You have cancelled or let expire too many reservations for this event. Please try again later."
        )

    owned = db.scalar(
        select(func.coalesce(func.sum(PurchaseItem.quantity - PurchaseItem.refunded_quantity), 0))
        .join(Purchase, PurchaseItem.purchase_id == Purchase.id)
        .where(
            Purchase.user_id == user.id,
            Purchase.event_id == event_id,
            Purchase.status.in_((PurchaseStatus.COMPLETED, PurchaseStatus.PARTIALLY_REFUNDED)),
        )
    )
    owned = owned or 0
    if owned + requested > settings.max_tickets_per_user_per_event:
        left = max(settings.max_tickets_per_user_per_event - owned, 0)
        raise TicketLimitExceededError(
            f"A customer may hold at most {settings.max_tickets_per_user_per_event} tickets per event; "
            f"you can buy {left} more."
        )


def reserve_tickets(
    db: Session,
    user: User,
    event_id: int,
    request: PurchaseRequest,
    settings: Settings,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> tuple[Purchase, bool]:
    """Hold tickets for a few minutes while the user completes the purchase.

    Returns (purchase, created). Capacity is taken with one conditional UPDATE
    per ticket type:

        UPDATE ticket_types SET held = held + :qty
        WHERE id = :id AND held + sold + :qty <= capacity

    PostgreSQL row locks make concurrent UPDATEs on the same row wait and
    re-check the WHERE clause, so two buyers can never both take the last
    ticket. All holds and the purchase rows commit in a single transaction.
    """
    now = now or datetime.now(UTC)

    if idempotency_key:
        existing = _find_by_idempotency_key(db, user.id, idempotency_key)
        if existing is not None:
            if existing.event_id != event_id:
                raise ConflictError("This Idempotency-Key was already used for a different purchase.")
            return existing, False

    # Give back stale holds first so availability does not depend on the
    # background sweeper having run.
    release_expired_purchases(db, now, event_id=event_id)

    event = db.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    if event.starts_at <= now:
        raise SalesClosedError("Ticket sales are closed: the event has already started.")

    requested_ids = [item.ticket_type_id for item in request.items]
    if len(set(requested_ids)) != len(requested_ids):
        raise InvalidPurchaseError("Each ticket type may appear only once per purchase.")
    total_quantity = sum(item.quantity for item in request.items)
    if total_quantity > settings.max_tickets_per_purchase:
        raise InvalidPurchaseError(f"A purchase may contain at most {settings.max_tickets_per_purchase} tickets.")

    names = {tt.id: tt.name for tt in event.ticket_types}
    for ticket_type_id in requested_ids:
        if ticket_type_id not in names:
            raise NotFoundError(f"Ticket type {ticket_type_id} does not belong to event {event_id}.")

    pending = db.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id,
            Purchase.event_id == event_id,
            Purchase.status == PurchaseStatus.PENDING,
        )
    )
    if pending is not None:
        if idempotency_key and pending.idempotency_key == idempotency_key:
            # A concurrent retry of this same request committed first.
            return pending, False
        raise PendingReservationExistsError(
            f"You already have a pending reservation for this event (purchase {pending.id}). "
            "Confirm or cancel it first."
        )

    _check_customer_limits(db, user, event_id, total_quantity, settings, now)

    purchase = Purchase(
        user_id=user.id,
        event_id=event_id,
        status=PurchaseStatus.PENDING,
        idempotency_key=idempotency_key,
        expires_at=now + timedelta(minutes=settings.reservation_ttl_minutes),
    )
    total = Decimal("0.00")
    for item in sorted(request.items, key=lambda i: i.ticket_type_id):
        unit_price = db.scalar(
            update(TicketType)
            .where(
                TicketType.id == item.ticket_type_id,
                TicketType.held + TicketType.sold + item.quantity <= TicketType.capacity,
            )
            .values(held=TicketType.held + item.quantity)
            .returning(TicketType.price)
        )
        if unit_price is None:
            db.rollback()
            raise InsufficientCapacityError(
                f"Not enough '{names[item.ticket_type_id]}' tickets available for {item.quantity} ticket(s)."
            )
        purchase.items.append(
            PurchaseItem(ticket_type_id=item.ticket_type_id, quantity=item.quantity, unit_price=unit_price)
        )
        total += unit_price * item.quantity

    purchase.total_amount = total
    db.add(purchase)
    try:
        db.commit()
    except IntegrityError as exc:
        # Lost a race against another request from the same user. Rolling back
        # also returns the tickets this request had just held.
        db.rollback()
        if idempotency_key:
            existing = _find_by_idempotency_key(db, user.id, idempotency_key)
            if existing is not None:
                return existing, False
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == ONE_PENDING_CONSTRAINT:
            raise PendingReservationExistsError(
                "You already have a pending reservation for this event. Confirm or cancel it first."
            ) from None
        raise
    audit(
        "reservation_created",
        purchase_id=purchase.id,
        user_id=user.id,
        event_id=event_id,
        tickets=total_quantity,
        total=purchase.total_amount,
    )
    return purchase, True


def confirm_purchase(db: Session, user: User, purchase_id: int, now: datetime | None = None) -> Purchase:
    """Turn a pending reservation into a completed purchase (held -> sold).

    Payment is out of scope: confirming stands in for "payment approved".
    Confirming an already completed purchase is a no-op, so retries are safe.
    """
    now = now or datetime.now(UTC)
    purchase = lock_purchase(db, user, purchase_id)

    if purchase.status == PurchaseStatus.COMPLETED:
        return purchase
    if purchase.status in (PurchaseStatus.EXPIRED, PurchaseStatus.CANCELLED):
        raise ReservationExpiredError(f"This reservation is {purchase.status.value} and can no longer be confirmed.")
    if purchase.status != PurchaseStatus.PENDING:
        raise InvalidPurchaseStateError("This purchase has already been confirmed.")
    if purchase.expires_at <= now:
        _release_hold(db, purchase, PurchaseStatus.EXPIRED)
        db.commit()
        audit("reservation_expired", purchase_id=purchase.id, user_id=user.id)
        raise ReservationExpiredError("This reservation has expired and its tickets were released.")
    if purchase.event.starts_at <= now:
        raise SalesClosedError("Ticket sales are closed: the event has already started.")

    for item in sorted(purchase.items, key=lambda i: i.ticket_type_id):
        db.execute(
            update(TicketType)
            .where(TicketType.id == item.ticket_type_id)
            .values(held=TicketType.held - item.quantity, sold=TicketType.sold + item.quantity)
        )
    purchase.status = PurchaseStatus.COMPLETED
    purchase.confirmed_at = now
    db.commit()
    audit("purchase_confirmed", purchase_id=purchase.id, user_id=user.id, total=purchase.total_amount)
    return purchase


def cancel_purchase(db: Session, user: User, purchase_id: int) -> Purchase:
    """Give up a pending reservation and release its tickets immediately."""
    purchase = lock_purchase(db, user, purchase_id)

    if purchase.status == PurchaseStatus.CANCELLED:
        return purchase
    if purchase.status != PurchaseStatus.PENDING:
        raise InvalidPurchaseStateError(
            "Only pending reservations can be cancelled. To return confirmed tickets, request a refund."
        )
    _release_hold(db, purchase, PurchaseStatus.CANCELLED)
    db.commit()
    audit("reservation_cancelled", purchase_id=purchase.id, user_id=user.id)
    return purchase
