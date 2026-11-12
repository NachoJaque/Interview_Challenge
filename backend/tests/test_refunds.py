from datetime import timedelta
from decimal import Decimal

import pytest

from app.exceptions import InvalidPurchaseError, NotFoundError, RefundNotAllowedError
from app.models import Purchase, PurchaseStatus
from app.schemas import PurchaseRequest, RefundRequest
from app.services.purchases import confirm_purchase, reserve_tickets
from app.services.refunds import create_refund
from tests.conftest import counters, ticket_type


def refund_of(*pairs: tuple[int, int], reason: str | None = None) -> RefundRequest:
    return RefundRequest(
        items=[{"purchase_item_id": item_id, "quantity": qty} for item_id, qty in pairs], reason=reason
    )


@pytest.fixture
def buy(db, settings):
    """Reserve and confirm in one step: returns a completed purchase."""

    def _buy(user, event, **quantities: int) -> Purchase:
        request = PurchaseRequest(
            items=[{"ticket_type_id": ticket_type(event, name).id, "quantity": qty} for name, qty in quantities.items()]
        )
        purchase, _ = reserve_tickets(db, user, event.id, request, settings)
        return confirm_purchase(db, user, purchase.id)

    return _buy


def item(purchase: Purchase, name: str):
    return next(i for i in purchase.items if i.ticket_type.name == name)


# --- Amounts by time window ---------------------------------------------------


def test_full_refund_more_than_seven_days_before_the_event(db, user, make_event, settings, buy):
    event = make_event(starts_in=timedelta(days=30))
    purchase = buy(user, event, General=2)
    general = ticket_type(event, "General")

    refund, _ = create_refund(db, user, purchase.id, refund_of((item(purchase, "General").id, 2)), settings)

    assert refund.refund_percentage == 100
    assert refund.amount == Decimal("100.00")
    assert purchase.status == PurchaseStatus.REFUNDED
    assert counters(general.id) == (0, 0)  # both tickets are back on sale


def test_half_refund_between_48_hours_and_seven_days_before_the_event(db, user, make_event, settings, buy):
    event = make_event(starts_in=timedelta(days=4))
    purchase = buy(user, event, VIP=1)

    refund, _ = create_refund(db, user, purchase.id, refund_of((item(purchase, "VIP").id, 1)), settings)

    assert refund.refund_percentage == 50
    assert refund.amount == Decimal("75.00")


def test_no_refund_less_than_48_hours_before_the_event(db, user, make_event, settings, buy):
    event = make_event(starts_in=timedelta(hours=20))
    purchase = buy(user, event, General=1)
    general = ticket_type(event, "General")

    with pytest.raises(RefundNotAllowedError):
        create_refund(db, user, purchase.id, refund_of((item(purchase, "General").id, 1)), settings)

    assert counters(general.id) == (0, 1)


def test_no_refund_after_the_event_started(db, user, make_event, settings, buy):
    event = make_event(starts_in=timedelta(days=30))
    purchase = buy(user, event, General=1)
    after_start = event.starts_at + timedelta(minutes=1)

    with pytest.raises(RefundNotAllowedError):
        create_refund(db, user, purchase.id, refund_of((item(purchase, "General").id, 1)), settings, now=after_start)


def test_refund_uses_the_price_paid_not_the_current_price(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=1)
    ticket_type(event, "General").price = Decimal("500.00")
    db.commit()

    refund, _ = create_refund(db, user, purchase.id, refund_of((item(purchase, "General").id, 1)), settings)

    assert refund.amount == Decimal("50.00")


# --- Partial refunds ----------------------------------------------------------


def test_partial_refund_releases_only_the_refunded_tickets(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=5)
    general = ticket_type(event, "General")

    refund, _ = create_refund(db, user, purchase.id, refund_of((item(purchase, "General").id, 2)), settings)

    assert refund.amount == Decimal("100.00")
    assert purchase.status == PurchaseStatus.PARTIALLY_REFUNDED
    assert item(purchase, "General").refunded_quantity == 2
    assert counters(general.id) == (0, 3)


def test_several_partial_refunds_until_fully_refunded(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=3, VIP=1)
    general_item, vip_item = item(purchase, "General"), item(purchase, "VIP")

    create_refund(db, user, purchase.id, refund_of((general_item.id, 1)), settings)
    create_refund(db, user, purchase.id, refund_of((vip_item.id, 1)), settings)
    assert purchase.status == PurchaseStatus.PARTIALLY_REFUNDED

    create_refund(db, user, purchase.id, refund_of((general_item.id, 2)), settings)

    assert purchase.status == PurchaseStatus.REFUNDED
    assert len(purchase.refunds) == 3
    assert purchase.refunded_amount == purchase.total_amount == Decimal("300.00")
    assert counters(ticket_type(event, "General").id) == (0, 0)
    assert counters(ticket_type(event, "VIP").id) == (0, 0)


def test_one_refund_can_cover_several_ticket_types(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=2, VIP=2)

    refund, _ = create_refund(
        db,
        user,
        purchase.id,
        refund_of((item(purchase, "General").id, 1), (item(purchase, "VIP").id, 2), reason="Change of plans"),
        settings,
    )

    assert refund.amount == Decimal("350.00")  # 50 + 2 x 150
    assert refund.reason == "Change of plans"
    assert [ri.quantity for ri in refund.items] == [1, 2]


def test_cannot_refund_more_tickets_than_remain(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=3)
    general_item = item(purchase, "General")
    create_refund(db, user, purchase.id, refund_of((general_item.id, 2)), settings)

    with pytest.raises(RefundNotAllowedError):
        create_refund(db, user, purchase.id, refund_of((general_item.id, 2)), settings)

    assert counters(ticket_type(event, "General").id) == (0, 1)


def test_fully_refunded_purchase_cannot_be_refunded_again(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=1)
    general_item = item(purchase, "General")
    create_refund(db, user, purchase.id, refund_of((general_item.id, 1)), settings)

    with pytest.raises(RefundNotAllowedError):
        create_refund(db, user, purchase.id, refund_of((general_item.id, 1)), settings)


# --- Who and what can be refunded --------------------------------------------


def test_non_refundable_ticket_types_are_rejected(db, user, make_event, settings, buy):
    event = make_event(vip_refundable=False)
    purchase = buy(user, event, General=1, VIP=1)

    with pytest.raises(RefundNotAllowedError):
        create_refund(db, user, purchase.id, refund_of((item(purchase, "VIP").id, 1)), settings)

    # The refundable ticket in the same purchase is unaffected.
    refund, _ = create_refund(db, user, purchase.id, refund_of((item(purchase, "General").id, 1)), settings)
    assert refund.amount == Decimal("50.00")


def test_a_rejected_refund_changes_nothing(db, user, make_event, settings, buy):
    """One invalid line must reject the whole request, including its valid lines."""
    event = make_event(vip_refundable=False)
    purchase = buy(user, event, General=2, VIP=1)

    with pytest.raises(RefundNotAllowedError):
        create_refund(
            db,
            user,
            purchase.id,
            refund_of((item(purchase, "General").id, 2), (item(purchase, "VIP").id, 1)),
            settings,
        )

    db.rollback()
    db.refresh(purchase)
    assert purchase.status == PurchaseStatus.COMPLETED
    assert purchase.refunds == []
    assert counters(ticket_type(event, "General").id) == (0, 2)


def test_cannot_refund_someone_elses_purchase(db, make_user, make_event, settings, buy):
    owner, intruder = make_user(), make_user()
    event = make_event()
    purchase = buy(owner, event, General=1)

    with pytest.raises(NotFoundError):
        create_refund(db, intruder, purchase.id, refund_of((item(purchase, "General").id, 1)), settings)


def test_cannot_refund_an_unconfirmed_reservation(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(
        db, user, event.id, PurchaseRequest(items=[{"ticket_type_id": general.id, "quantity": 1}]), settings
    )

    with pytest.raises(RefundNotAllowedError):
        create_refund(db, user, purchase.id, refund_of((purchase.items[0].id, 1)), settings)

    assert counters(general.id) == (1, 0)


def test_item_must_belong_to_the_purchase(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=1)
    other_purchase = buy(user, make_event(), General=1)

    with pytest.raises(NotFoundError):
        create_refund(db, user, purchase.id, refund_of((item(other_purchase, "General").id, 1)), settings)


def test_item_cannot_be_repeated_in_one_refund(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=3)
    general_item = item(purchase, "General")

    with pytest.raises(InvalidPurchaseError):
        create_refund(db, user, purchase.id, refund_of((general_item.id, 1), (general_item.id, 1)), settings)
