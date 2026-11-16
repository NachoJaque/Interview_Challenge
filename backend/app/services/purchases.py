"""Purchase lifecycle: reserve -> confirm, or cancel / expire.

Locking rules shared by every function here (and by services/refunds.py), so
that no two transactions can deadlock:

1. Lock the purchase row first (SELECT ... FOR UPDATE), then ticket types.
2. Touch ticket_type rows in ascending id order.
3. Hold locks for one purchase per transaction.
"""

from collections.abc import Iterable
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
    ReservationCancelledError,
    ReservationExpiredError,
    SalesClosedError,
    TicketLimitExceededError,
    TooManyReservationsError,
)
from app.models import Event, Purchase, PurchaseItem, PurchaseStatus, TicketType, User
from app.schemas import PurchaseRequest

ONE_PENDING_CONSTRAINT = "uq_purchases_one_pending_per_user_event"
ABANDONED_STATUSES = (PurchaseStatus.CANCELLED, PurchaseStatus.EXPIRED)
OWNED_STATUSES = (PurchaseStatus.COMPLETED, PurchaseStatus.PARTIALLY_REFUNDED)


# --- Shared building blocks -----------------------------------------------------


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


def move_tickets(db: Session, quantities: Iterable[tuple[int, int]], *, held: int = 0, sold: int = 0) -> None:
    """Shift each ticket type's counters by its quantity, in the given directions.

    `quantities` holds (ticket_type_id, quantity) pairs; `held` and `sold` are
    -1, 0 or +1. Confirming, for instance, is `held=-1, sold=+1`.

    This is the one place that walks ticket types in ascending id order
    (locking rule 2). The caller must hold the purchase lock (rule 1).
    """
    for ticket_type_id, quantity in sorted(quantities):
        db.execute(
            update(TicketType)
            .where(TicketType.id == ticket_type_id)
            .values(held=TicketType.held + held * quantity, sold=TicketType.sold + sold * quantity)
        )


def _item_quantities(purchase: Purchase) -> list[tuple[int, int]]:
    return [(item.ticket_type_id, item.quantity) for item in purchase.items]


def _release_hold(db: Session, purchase: Purchase, new_status: PurchaseStatus) -> None:
    """Return a pending purchase's held tickets to the pool."""
    move_tickets(db, _item_quantities(purchase), held=-1)
    purchase.status = new_status


def _violated_constraint(exc: IntegrityError) -> str | None:
    """Name of the database constraint behind an IntegrityError (psycopg reports it)."""
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


# --- Expiry ---------------------------------------------------------------------


def release_expired_purchases(
    db: Session, now: datetime | None = None, event_id: int | None = None, limit: int | None = None
) -> int:
    """Expire pending reservations whose hold has run out. Returns how many were released.

    `limit` caps the work of one call (oldest first). The background sweeper
    runs without one; a purchase request passes a small one.

    Safe to run from several workers at once: each purchase is released in its
    own transaction, and SKIP LOCKED skips rows another transaction is busy
    with (e.g. a confirmation in progress, which then decides the outcome).
    """
    now = now or datetime.now(UTC)
    expired = (Purchase.status == PurchaseStatus.PENDING) & (Purchase.expires_at <= now)

    candidates = select(Purchase.id).where(expired)
    if event_id is not None:
        candidates = candidates.where(Purchase.event_id == event_id)
    if limit is not None:
        candidates = candidates.order_by(Purchase.expires_at).limit(limit)
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


# --- Reserving ------------------------------------------------------------------


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

    Returns (purchase, created); `created` is False when an idempotent retry
    got back the reservation it had already made.
    """
    now = now or datetime.now(UTC)

    replayed = _find_replay(db, user, event_id, idempotency_key)
    if replayed is not None:
        return replayed, False

    # Give back a few stale holds first, so availability does not depend on
    # the background sweeper having run. Only a few: after a big sale thousands
    # expire together, and no buyer should have to release them all.
    release_expired_purchases(db, now, event_id=event_id, limit=settings.reservation_lazy_release_limit)

    event = _get_event_on_sale(db, event_id, now)
    total_quantity = _validate_request(request, event, settings)

    pending = _find_pending(db, user, event_id)
    if pending is not None:
        if idempotency_key and pending.idempotency_key == idempotency_key:
            return pending, False  # a concurrent retry of this same request committed first
        raise PendingReservationExistsError(
            f"You already have a pending reservation for this event (purchase {pending.id}). "
            "Confirm or cancel it first."
        )

    _check_abandoned_reservations(db, user, event_id, settings, now)
    _check_ticket_limit(db, user, event_id, total_quantity, settings)

    purchase = Purchase(
        user_id=user.id,
        event_id=event_id,
        status=PurchaseStatus.PENDING,
        idempotency_key=idempotency_key,
        expires_at=now + timedelta(minutes=settings.reservation_ttl_minutes),
        items=_hold_tickets(db, event, request),
    )
    purchase.total_amount = sum((item.unit_price * item.quantity for item in purchase.items), Decimal("0.00"))
    db.add(purchase)

    winner = _commit_reservation(db, user, idempotency_key)
    if winner is not None:
        return winner, False

    audit(
        "reservation_created",
        purchase_id=purchase.id,
        user_id=user.id,
        event_id=event_id,
        tickets=total_quantity,
        total=purchase.total_amount,
    )
    return purchase, True


def _find_by_idempotency_key(db: Session, user_id: int, key: str) -> Purchase | None:
    return db.scalar(select(Purchase).where(Purchase.user_id == user_id, Purchase.idempotency_key == key))


def _find_replay(db: Session, user: User, event_id: int, idempotency_key: str | None) -> Purchase | None:
    """The purchase an earlier request with this key created, if any."""
    if not idempotency_key:
        return None
    existing = _find_by_idempotency_key(db, user.id, idempotency_key)
    if existing is not None and existing.event_id != event_id:
        raise ConflictError("This Idempotency-Key was already used for a different purchase.")
    return existing


def _get_event_on_sale(db: Session, event_id: int, now: datetime) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    _ensure_sales_open(event, now)
    return event


def _ensure_sales_open(event: Event, now: datetime) -> None:
    if event.starts_at <= now:
        raise SalesClosedError("Ticket sales are closed: the event has already started.")


def _validate_request(request: PurchaseRequest, event: Event, settings: Settings) -> int:
    """Check the request against the event. Returns the total number of tickets."""
    requested_ids = [item.ticket_type_id for item in request.items]
    if len(set(requested_ids)) != len(requested_ids):
        raise InvalidPurchaseError("Each ticket type may appear only once per purchase.")

    total_quantity = sum(item.quantity for item in request.items)
    if total_quantity > settings.max_tickets_per_purchase:
        raise InvalidPurchaseError(f"A purchase may contain at most {settings.max_tickets_per_purchase} tickets.")

    event_ticket_type_ids = {ticket_type.id for ticket_type in event.ticket_types}
    for ticket_type_id in requested_ids:
        if ticket_type_id not in event_ticket_type_ids:
            raise NotFoundError(f"Ticket type {ticket_type_id} does not belong to event {event.id}.")
    return total_quantity


def _find_pending(db: Session, user: User, event_id: int) -> Purchase | None:
    return db.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id,
            Purchase.event_id == event_id,
            Purchase.status == PurchaseStatus.PENDING,
        )
    )


def _check_abandoned_reservations(db: Session, user: User, event_id: int, settings: Settings, now: datetime) -> None:
    """Refuse customers who keep reserving this event's tickets without buying them."""
    window_start = now - timedelta(minutes=settings.abandoned_reservation_window_minutes)
    abandoned = db.scalar(
        select(func.count())
        .select_from(Purchase)
        .where(
            Purchase.user_id == user.id,
            Purchase.event_id == event_id,
            Purchase.status.in_(ABANDONED_STATUSES),
            Purchase.created_at >= window_start,
        )
    )
    assert abandoned is not None  # COUNT never returns NULL
    if abandoned >= settings.max_abandoned_reservations:
        raise TooManyReservationsError(
            "You have cancelled or let expire too many reservations for this event. Please try again later."
        )


def _check_ticket_limit(db: Session, user: User, event_id: int, requested: int, settings: Settings) -> None:
    """Cap the tickets one customer holds for an event across all their purchases.

    The caller has established that the user has no pending reservation for
    the event, and the partial unique index keeps two concurrent requests
    from the same user from both getting past that check.
    """
    owned = db.scalar(
        select(func.coalesce(func.sum(PurchaseItem.quantity - PurchaseItem.refunded_quantity), 0))
        .join(Purchase, PurchaseItem.purchase_id == Purchase.id)
        .where(
            Purchase.user_id == user.id,
            Purchase.event_id == event_id,
            Purchase.status.in_(OWNED_STATUSES),
        )
    )
    assert owned is not None  # COALESCE never returns NULL
    limit = settings.max_tickets_per_user_per_event
    if owned + requested > limit:
        raise TicketLimitExceededError(
            f"A customer may hold at most {limit} tickets per event; you can buy {max(limit - owned, 0)} more."
        )


def _hold_tickets(db: Session, event: Event, request: PurchaseRequest) -> list[PurchaseItem]:
    """Take capacity for every requested ticket type, or for none of them.

    One conditional UPDATE per ticket type checks and takes capacity at once:

        UPDATE ticket_types SET held = held + :qty
        WHERE id = :id AND held + sold + :qty <= capacity
        RETURNING price

    PostgreSQL row locks make concurrent UPDATEs on the same row wait and
    re-check the WHERE clause, so two buyers can never both take the last
    ticket. The holds commit together with the purchase.
    """
    names = {ticket_type.id: ticket_type.name for ticket_type in event.ticket_types}
    items = []
    for requested in sorted(request.items, key=lambda item: item.ticket_type_id):  # locking rule 2
        unit_price = db.scalar(
            update(TicketType)
            .where(
                TicketType.id == requested.ticket_type_id,
                TicketType.held + TicketType.sold + requested.quantity <= TicketType.capacity,
            )
            .values(held=TicketType.held + requested.quantity)
            .returning(TicketType.price)
        )
        if unit_price is None:
            db.rollback()  # gives back what this request had already held
            raise InsufficientCapacityError(
                f"Not enough '{names[requested.ticket_type_id]}' tickets available for {requested.quantity} ticket(s)."
            )
        items.append(
            PurchaseItem(ticket_type_id=requested.ticket_type_id, quantity=requested.quantity, unit_price=unit_price)
        )
    return items


def _commit_reservation(db: Session, user: User, idempotency_key: str | None) -> Purchase | None:
    """Commit the new reservation.

    Returns None on success. If another request from the same user committed
    first, the unique indexes reject this one: rolling back also returns the
    tickets it had held, and the result is that earlier purchase (same
    idempotency key) or an error (a different reservation for the event).
    """
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        winner = _find_by_idempotency_key(db, user.id, idempotency_key) if idempotency_key else None
        if winner is not None:
            return winner
        if _violated_constraint(exc) == ONE_PENDING_CONSTRAINT:
            raise PendingReservationExistsError(
                "You already have a pending reservation for this event. Confirm or cancel it first."
            ) from None
        raise
    return None


# --- Confirming and cancelling ----------------------------------------------------


def confirm_purchase(db: Session, user: User, purchase_id: int, now: datetime | None = None) -> Purchase:
    """Turn a pending reservation into a completed purchase (held -> sold).

    Payment is out of scope: confirming stands in for "payment approved".
    Confirming an already completed purchase is a no-op, so retries are safe.
    """
    now = now or datetime.now(UTC)
    purchase = lock_purchase(db, user, purchase_id)

    if purchase.status == PurchaseStatus.COMPLETED:
        return purchase
    if purchase.status == PurchaseStatus.CANCELLED:
        raise ReservationCancelledError("This reservation was cancelled and can no longer be confirmed.")
    if purchase.status == PurchaseStatus.EXPIRED:
        raise ReservationExpiredError("This reservation has expired and can no longer be confirmed.")
    if purchase.status != PurchaseStatus.PENDING:
        raise InvalidPurchaseStateError("This purchase has already been confirmed.")
    if purchase.expires_at <= now:
        _release_hold(db, purchase, PurchaseStatus.EXPIRED)
        db.commit()
        audit("reservation_expired", purchase_id=purchase.id, user_id=user.id)
        raise ReservationExpiredError("This reservation has expired and its tickets were released.")
    _ensure_sales_open(purchase.event, now)

    move_tickets(db, _item_quantities(purchase), held=-1, sold=+1)
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
