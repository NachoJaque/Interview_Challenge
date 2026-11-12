from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.deps import AppSettings, CurrentUser, DbSession, ResourceId
from app.exceptions import NotFoundError
from app.models import Purchase, PurchaseItem
from app.schemas import (
    PurchaseItemRead,
    PurchaseRead,
    PurchaseRequest,
    RefundPolicyRead,
    RefundRead,
    RefundRequest,
)
from app.services import purchases as purchase_service
from app.services import refunds as refund_service

router = APIRouter(tags=["purchases"])

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=100,
        description="Optional client-generated key. Retrying with the same key returns the "
        "original result instead of reserving or refunding a second time.",
    ),
]

_purchase_loading = (
    selectinload(Purchase.event),
    selectinload(Purchase.items).selectinload(PurchaseItem.ticket_type),
    selectinload(Purchase.refunds),
)


def _to_read(purchase: Purchase, settings: Settings) -> PurchaseRead:
    return PurchaseRead(
        id=purchase.id,
        event_id=purchase.event_id,
        event_name=purchase.event.name,
        event_starts_at=purchase.event.starts_at,
        status=purchase.status,
        total_amount=purchase.total_amount,
        refunded_amount=purchase.refunded_amount,
        created_at=purchase.created_at,
        expires_at=purchase.expires_at,
        confirmed_at=purchase.confirmed_at,
        items=[
            PurchaseItemRead(
                id=item.id,
                ticket_type_id=item.ticket_type_id,
                ticket_type_name=item.ticket_type.name,
                quantity=item.quantity,
                refunded_quantity=item.refunded_quantity,
                refundable_quantity=item.refundable_quantity,
                unit_price=item.unit_price,
                is_refundable=item.ticket_type.is_refundable,
            )
            for item in purchase.items
        ],
        refunds=[RefundRead.model_validate(refund) for refund in purchase.refunds],
        refund_policy=RefundPolicyRead.model_validate(refund_service.current_refund_policy(purchase, settings)),
    )


@router.post("/events/{event_id}/purchases", response_model=PurchaseRead, status_code=status.HTTP_201_CREATED)
def reserve_tickets(
    event_id: ResourceId,
    payload: PurchaseRequest,
    response: Response,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    idempotency_key: IdempotencyKey = None,
) -> PurchaseRead:
    """Reserve tickets for an event.

    The tickets are held for a few minutes (`expires_at`) as a `pending`
    purchase. Call `POST /purchases/{id}/confirm` to complete the purchase,
    or `POST /purchases/{id}/cancel` to release them.
    """
    purchase, created = purchase_service.reserve_tickets(db, user, event_id, payload, settings, idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    return _to_read(purchase, settings)


@router.post("/purchases/{purchase_id}/confirm", response_model=PurchaseRead)
def confirm_purchase(purchase_id: ResourceId, db: DbSession, user: CurrentUser, settings: AppSettings) -> PurchaseRead:
    """Complete a pending reservation: its held tickets become sold."""
    return _to_read(purchase_service.confirm_purchase(db, user, purchase_id), settings)


@router.post("/purchases/{purchase_id}/cancel", response_model=PurchaseRead)
def cancel_purchase(purchase_id: ResourceId, db: DbSession, user: CurrentUser, settings: AppSettings) -> PurchaseRead:
    """Cancel a pending reservation and release its tickets."""
    return _to_read(purchase_service.cancel_purchase(db, user, purchase_id), settings)


@router.post("/purchases/{purchase_id}/refunds", response_model=PurchaseRead, status_code=status.HTTP_201_CREATED)
def refund_tickets(
    purchase_id: ResourceId,
    payload: RefundRequest,
    response: Response,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    idempotency_key: IdempotencyKey = None,
) -> PurchaseRead:
    """Refund some or all tickets of a confirmed purchase (see README "Refund rules").

    Refunded tickets go back on sale immediately. Returns the updated purchase.
    """
    refund, created = refund_service.create_refund(db, user, purchase_id, payload, settings, idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    return _to_read(refund.purchase, settings)


@router.get("/purchases", response_model=list[PurchaseRead])
def list_my_purchases(
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> list[PurchaseRead]:
    """The caller's purchases, newest first."""
    purchases = db.scalars(
        select(Purchase)
        .where(Purchase.user_id == user.id)
        .options(*_purchase_loading)
        .order_by(Purchase.created_at.desc(), Purchase.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_to_read(p, settings) for p in purchases]


@router.get("/purchases/{purchase_id}", response_model=PurchaseRead)
def get_my_purchase(purchase_id: ResourceId, db: DbSession, user: CurrentUser, settings: AppSettings) -> PurchaseRead:
    purchase = db.scalar(
        select(Purchase).where(Purchase.id == purchase_id, Purchase.user_id == user.id).options(*_purchase_loading)
    )
    if purchase is None:
        raise NotFoundError(f"Purchase {purchase_id} not found.")
    return _to_read(purchase, settings)
