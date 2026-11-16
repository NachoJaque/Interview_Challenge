"""Race conditions, exercised with real threads and one database session per
thread, the same way concurrent HTTP requests hit the service layer."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Purchase, PurchaseStatus, User
from app.schemas import PurchaseRequest, RefundRequest
from app.services.purchases import confirm_purchase, release_expired_purchases, reserve_tickets
from app.services.refunds import create_refund
from tests.conftest import counters, refund_of, run_concurrently, ticket_type


def buy_one(user_id: int, event_id: int, ticket_type_id: int, settings) -> Callable[[Session], object]:
    request = PurchaseRequest(items=[{"ticket_type_id": ticket_type_id, "quantity": 1}])
    return lambda session: reserve_tickets(session, session.get(User, user_id), event_id, request, settings)


def test_last_tickets_are_never_oversold(make_user, make_event, settings):
    event = make_event(vip_capacity=5)
    vip = ticket_type(event, "VIP")
    buyers = [make_user() for _ in range(30)]

    results = run_concurrently([buy_one(buyer.id, event.id, vip.id, settings) for buyer in buyers])

    assert results == {"ok": 5, "insufficient_capacity": 25}
    assert counters(vip.id) == (5, 0)


def test_multi_type_reservations_in_opposite_order_do_not_deadlock(make_user, make_event, settings):
    event = make_event(general_capacity=10, vip_capacity=10)
    general, vip = ticket_type(event, "General"), ticket_type(event, "VIP")

    def reserve_both(user_id: int, first: int, second: int) -> Callable[[Session], object]:
        request = PurchaseRequest(
            items=[{"ticket_type_id": first, "quantity": 1}, {"ticket_type_id": second, "quantity": 1}]
        )
        return lambda session: reserve_tickets(session, session.get(User, user_id), event.id, request, settings)

    tasks = []
    for index in range(20):
        order = (general.id, vip.id) if index % 2 == 0 else (vip.id, general.id)
        tasks.append(reserve_both(make_user().id, *order))

    results = run_concurrently(tasks)

    assert results == {"ok": 10, "insufficient_capacity": 10}
    assert counters(general.id) == (10, 0)
    assert counters(vip.id) == (10, 0)


def test_same_user_cannot_open_two_reservations_at_once(user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")

    results = run_concurrently([buy_one(user.id, event.id, general.id, settings) for _ in range(8)])

    assert results == {"ok": 1, "pending_reservation_exists": 7}
    assert counters(general.id) == (1, 0)


def test_concurrent_retries_with_one_idempotency_key_create_one_reservation(user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    request = PurchaseRequest(items=[{"ticket_type_id": general.id, "quantity": 2}])

    def retry(session: Session) -> object:
        return reserve_tickets(
            session, session.get(User, user.id), event.id, request, settings, idempotency_key="same-key-123"
        )

    results = run_concurrently([retry] * 8)

    assert results == {"ok": 8}  # every retry gets the same reservation back
    with SessionLocal() as session:
        assert session.query(Purchase).count() == 1
    assert counters(general.id) == (2, 0)


def test_concurrent_confirmations_sell_the_tickets_once(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(
        db, user, event.id, PurchaseRequest(items=[{"ticket_type_id": general.id, "quantity": 3}]), settings
    )

    results = run_concurrently([lambda session: confirm_purchase(session, session.get(User, user.id), purchase.id)] * 8)

    assert results == {"ok": 8}
    assert counters(general.id) == (0, 3)


def test_concurrent_refunds_never_refund_the_same_ticket_twice(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(
        db, user, event.id, PurchaseRequest(items=[{"ticket_type_id": general.id, "quantity": 3}]), settings
    )
    confirm_purchase(db, user, purchase.id)
    request = RefundRequest(items=[{"purchase_item_id": purchase.items[0].id, "quantity": 1}])

    results = run_concurrently(
        [lambda session: create_refund(session, session.get(User, user.id), purchase.id, request, settings)] * 10
    )

    assert results == {"ok": 3, "refund_not_allowed": 7}
    assert counters(general.id) == (0, 0)
    db.refresh(purchase)
    assert purchase.status == PurchaseStatus.REFUNDED
    assert purchase.refunded_amount == purchase.total_amount


def test_confirmation_racing_the_sweeper_has_exactly_one_outcome(db, user, make_event, settings):
    """A reservation at its deadline is either sold or released, never both."""
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(
        db, user, event.id, PurchaseRequest(items=[{"ticket_type_id": general.id, "quantity": 2}]), settings
    )
    before_expiry = purchase.expires_at - timedelta(seconds=1)
    after_expiry = purchase.expires_at + timedelta(seconds=1)

    run_concurrently(
        [lambda session: confirm_purchase(session, session.get(User, user.id), purchase.id, now=before_expiry)] * 4
        + [lambda session: release_expired_purchases(session, after_expiry)] * 4
    )

    db.refresh(purchase)
    held, sold = counters(general.id)
    assert held == 0
    if purchase.status == PurchaseStatus.COMPLETED:
        assert sold == 2
    else:
        assert purchase.status == PurchaseStatus.EXPIRED
        assert sold == 0


def test_sweepers_running_in_parallel_release_each_reservation_once(db, make_user, make_event, settings):
    event = make_event(general_capacity=50)
    general = ticket_type(event, "General")
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    for _ in range(10):
        reserve_tickets(
            db,
            make_user(),
            event.id,
            PurchaseRequest(items=[{"ticket_type_id": general.id, "quantity": 2}]),
            settings,
            now=long_ago,
        )
    assert counters(general.id) == (20, 0)

    run_concurrently([lambda session: release_expired_purchases(session)] * 6)

    assert counters(general.id) == (0, 0)  # a double release would violate held >= 0


def test_concurrent_retries_of_one_refund_refund_once(db, user, make_event, settings, buy):
    event = make_event()
    purchase = buy(user, event, General=5)
    request = refund_of((purchase.items[0].id, 1))

    outcomes = run_concurrently(
        [
            lambda session: create_refund(
                session, session.get(User, user.id), purchase.id, request, settings, "same-refund-key"
            )
        ]
        * 8
    )

    assert outcomes == {"ok": 8}
    assert counters(ticket_type(event, "General").id) == (0, 4)
