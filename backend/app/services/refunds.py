from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import Settings
from app.exceptions import InvalidPurchaseError, NotFoundError, RefundNotAllowedError
from app.models import Purchase, PurchaseStatus, Refund, RefundItem, TicketType, User
from app.schemas import RefundRequest
from app.services.purchases import lock_purchase
from app.services.refund_policy import RefundDecision, evaluate_refund_window

CENT = Decimal("0.01")
REFUNDABLE_STATUSES = (PurchaseStatus.COMPLETED, PurchaseStatus.PARTIALLY_REFUNDED)


def current_refund_policy(purchase: Purchase, settings: Settings, now: datetime | None = None) -> RefundDecision:
    """What a refund requested right now would get for this purchase."""
    now = now or datetime.now(UTC)
    if purchase.status == PurchaseStatus.PENDING:
        return RefundDecision(False, 0, "The purchase has not been confirmed yet.")
    if purchase.status == PurchaseStatus.REFUNDED:
        return RefundDecision(False, 0, "The purchase has already been fully refunded.")
    if purchase.status not in REFUNDABLE_STATUSES:
        return RefundDecision(False, 0, f"The reservation is {purchase.status.value}; there is nothing to refund.")
    return evaluate_refund_window(purchase.event.starts_at, now, settings)


def create_refund(
    db: Session,
    user: User,
    purchase_id: int,
    request: RefundRequest,
    settings: Settings,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> tuple[Refund, bool]:
    """Refund some or all tickets of a confirmed purchase and release their capacity.

    Returns (refund, created). Retrying with the same idempotency key returns
    the original refund instead of refunding more tickets.

    The purchase row is locked (SELECT ... FOR UPDATE) for the whole
    transaction, so concurrent refund requests on the same purchase are
    serialized and can never refund the same ticket twice.
    """
    now = now or datetime.now(UTC)

    purchase = lock_purchase(db, user, purchase_id)

    if idempotency_key:
        # The purchase lock is held, so a concurrent retry waits here and then
        # finds the refund the first request committed.
        existing = db.scalar(
            select(Refund).where(Refund.purchase_id == purchase.id, Refund.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing, False

    requested_ids = [item.purchase_item_id for item in request.items]
    if len(set(requested_ids)) != len(requested_ids):
        raise InvalidPurchaseError("Each purchase item may appear only once per refund.")

    decision = current_refund_policy(purchase, settings, now)
    if not decision.allowed:
        raise RefundNotAllowedError(decision.reason)

    # Validate everything before mutating anything.
    items_by_id = {item.id: item for item in purchase.items}
    for req in request.items:
        item = items_by_id.get(req.purchase_item_id)
        if item is None:
            raise NotFoundError(f"Item {req.purchase_item_id} is not part of purchase {purchase_id}.")
        if not item.ticket_type.is_refundable:
            raise RefundNotAllowedError(f"'{item.ticket_type.name}' tickets are non-refundable.")
        if req.quantity > item.refundable_quantity:
            raise RefundNotAllowedError(
                f"Only {item.refundable_quantity} '{item.ticket_type.name}' ticket(s) can still be refunded."
            )

    refund = Refund(
        refund_percentage=decision.percentage,
        reason=request.reason,
        idempotency_key=idempotency_key,
        amount=Decimal("0.00"),
    )
    for req in sorted(request.items, key=lambda r: items_by_id[r.purchase_item_id].ticket_type_id):
        item = items_by_id[req.purchase_item_id]
        # Rounded per ticket, so refunding tickets one by one or together
        # always pays exactly the same.
        per_ticket = (item.unit_price * decision.percentage / 100).quantize(CENT, ROUND_HALF_UP)
        amount = per_ticket * req.quantity
        item.refunded_quantity += req.quantity
        db.execute(
            update(TicketType).where(TicketType.id == item.ticket_type_id).values(sold=TicketType.sold - req.quantity)
        )
        refund.items.append(RefundItem(purchase_item=item, quantity=req.quantity, amount=amount))
        refund.amount += amount

    purchase.refunds.append(refund)
    fully_refunded = all(item.refundable_quantity == 0 for item in purchase.items)
    purchase.status = PurchaseStatus.REFUNDED if fully_refunded else PurchaseStatus.PARTIALLY_REFUNDED
    db.commit()
    audit(
        "refund_created",
        refund_id=refund.id,
        purchase_id=purchase.id,
        user_id=user.id,
        tickets=sum(i.quantity for i in refund.items),
        amount=refund.amount,
        percentage=refund.refund_percentage,
    )
    return refund, True
