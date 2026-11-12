"""Rules added after the code review: customer limits, refund idempotency,
consistent rounding, and the smaller API fixes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest

from app.exceptions import SalesClosedError, TicketLimitExceededError, TooManyReservationsError
from app.models import Event, PurchaseStatus, TicketType, User
from app.schemas import PurchaseRequest, RefundRequest
from app.services.purchases import cancel_purchase, confirm_purchase, release_expired_purchases, reserve_tickets
from app.services.refunds import create_refund
from tests.conftest import auth_headers, counters, ticket_type
from tests.test_concurrency import run_concurrently


def request_for(*pairs: tuple[int, int]) -> PurchaseRequest:
    return PurchaseRequest(items=[{"ticket_type_id": tt_id, "quantity": qty} for tt_id, qty in pairs])


def refund_of(item_id: int, quantity: int) -> RefundRequest:
    return RefundRequest(items=[{"purchase_item_id": item_id, "quantity": quantity}])


def buy(db, user, event, settings, quantity: int, name: str = "General"):
    purchase, _ = reserve_tickets(db, user, event.id, request_for((ticket_type(event, name).id, quantity)), settings)
    return confirm_purchase(db, user, purchase.id)


# --- Holding tickets without buying -----------------------------------------


def test_repeatedly_abandoning_reservations_is_blocked(db, user, make_event, settings):
    event = make_event()
    vip = ticket_type(event, "VIP")

    for _ in range(settings.max_abandoned_reservations):
        purchase, _ = reserve_tickets(db, user, event.id, request_for((vip.id, 2)), settings)
        cancel_purchase(db, user, purchase.id)

    with pytest.raises(TooManyReservationsError):
        reserve_tickets(db, user, event.id, request_for((vip.id, 2)), settings)
    assert counters(vip.id) == (0, 0)


def test_expired_reservations_count_as_abandoned(db, user, make_event, settings):
    event = make_event()
    vip = ticket_type(event, "VIP")
    now = datetime.now(UTC)

    for i in range(settings.max_abandoned_reservations):
        start = now + timedelta(minutes=11 * i)
        reserve_tickets(db, user, event.id, request_for((vip.id, 1)), settings, now=start)
        release_expired_purchases(db, now=start + timedelta(minutes=10, seconds=1))

    with pytest.raises(TooManyReservationsError):
        reserve_tickets(db, user, event.id, request_for((vip.id, 1)), settings, now=now + timedelta(minutes=40))


def test_abandoned_reservations_are_forgotten_after_the_window(db, user, make_event, settings):
    event = make_event()
    vip = ticket_type(event, "VIP")
    for _ in range(settings.max_abandoned_reservations):
        purchase, _ = reserve_tickets(db, user, event.id, request_for((vip.id, 1)), settings)
        cancel_purchase(db, user, purchase.id)

    later = datetime.now(UTC) + timedelta(minutes=settings.abandoned_reservation_window_minutes + 1)
    purchase, created = reserve_tickets(db, user, event.id, request_for((vip.id, 1)), settings, now=later)

    assert created


def test_the_block_applies_per_event_and_per_customer(db, make_user, make_event, settings):
    hoarder, other = make_user(), make_user()
    event, other_event = make_event(), make_event()
    vip = ticket_type(event, "VIP")
    for _ in range(settings.max_abandoned_reservations):
        purchase, _ = reserve_tickets(db, hoarder, event.id, request_for((vip.id, 1)), settings)
        cancel_purchase(db, hoarder, purchase.id)

    assert reserve_tickets(db, other, event.id, request_for((vip.id, 1)), settings)[1]
    other_vip = ticket_type(other_event, "VIP")
    assert reserve_tickets(db, hoarder, other_event.id, request_for((other_vip.id, 1)), settings)[1]


def test_completed_purchases_do_not_count_as_abandoned(db, user, make_event, settings):
    event = make_event()
    for _ in range(settings.max_abandoned_reservations + 1):
        buy(db, user, event, settings, 1)

    assert counters(ticket_type(event, "General").id) == (0, settings.max_abandoned_reservations + 1)


# --- Tickets per customer and event ------------------------------------------


def test_a_customer_cannot_exceed_the_per_event_limit_across_purchases(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    buy(db, user, event, settings, 10)
    buy(db, user, event, settings, 10)

    with pytest.raises(TicketLimitExceededError):
        reserve_tickets(db, user, event.id, request_for((general.id, 1)), settings)
    assert counters(general.id) == (0, 20)


def test_refunded_tickets_free_room_under_the_limit(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    buy(db, user, event, settings, 10)
    purchase = buy(db, user, event, settings, 10)
    create_refund(db, user, purchase.id, refund_of(purchase.items[0].id, 4), settings)

    with pytest.raises(TicketLimitExceededError):
        reserve_tickets(db, user, event.id, request_for((general.id, 5)), settings)
    assert buy(db, user, event, settings, 4).status == PurchaseStatus.COMPLETED


def test_the_limit_is_per_customer(db, make_user, make_event, settings):
    event = make_event()
    first, second = make_user(), make_user()
    buy(db, first, event, settings, 10)
    buy(db, first, event, settings, 10)

    assert buy(db, second, event, settings, 10).status == PurchaseStatus.COMPLETED


# --- Refund idempotency -------------------------------------------------------


def test_retrying_a_refund_with_the_same_key_refunds_once(db, user, make_event, settings):
    event = make_event()
    purchase = buy(db, user, event, settings, 4)
    item_id = purchase.items[0].id

    first, created = create_refund(db, user, purchase.id, refund_of(item_id, 1), settings, "refund-key-1")
    again, created_again = create_refund(db, user, purchase.id, refund_of(item_id, 1), settings, "refund-key-1")

    assert created and not created_again
    assert again.id == first.id
    db.refresh(purchase)
    assert purchase.items[0].refunded_quantity == 1
    assert counters(ticket_type(event, "General").id) == (0, 3)


def test_a_different_key_is_a_different_refund(db, user, make_event, settings):
    event = make_event()
    purchase = buy(db, user, event, settings, 4)
    item_id = purchase.items[0].id

    create_refund(db, user, purchase.id, refund_of(item_id, 1), settings, "refund-key-1")
    create_refund(db, user, purchase.id, refund_of(item_id, 1), settings, "refund-key-2")

    db.refresh(purchase)
    assert purchase.items[0].refunded_quantity == 2


def test_concurrent_retries_of_one_refund_refund_once(db, user, make_event, settings):
    event = make_event()
    purchase = buy(db, user, event, settings, 5)
    request = refund_of(purchase.items[0].id, 1)

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


def test_refund_replay_over_http_returns_200_and_the_same_purchase(client, user, make_event):
    headers = auth_headers(user)
    event = make_event()
    general = ticket_type(event, "General")
    purchase = client.post(
        f"/events/{event.id}/purchases",
        json={"items": [{"ticket_type_id": general.id, "quantity": 3}]},
        headers=headers,
    ).json()
    client.post(f"/purchases/{purchase['id']}/confirm", headers=headers)
    body = {"items": [{"purchase_item_id": purchase["items"][0]["id"], "quantity": 1}]}
    keyed = {**headers, "Idempotency-Key": "refund-http-key"}

    first = client.post(f"/purchases/{purchase['id']}/refunds", json=body, headers=keyed)
    replay = client.post(f"/purchases/{purchase['id']}/refunds", json=body, headers=keyed)

    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.json()["refunded_amount"] == "50.00"
    assert len(replay.json()["refunds"]) == 1


# --- Rounding -------------------------------------------------------------------


def test_splitting_a_refund_pays_the_same_as_refunding_together(db, make_user, settings):
    event = Event(
        name="Odd cents",
        venue="Venue",
        starts_at=datetime.now(UTC) + timedelta(days=4),  # 50% window
        ticket_types=[TicketType(name="General", price=Decimal("25.25"), capacity=50)],
    )
    db.add(event)
    db.commit()
    splitter, bundler = make_user(), make_user()
    split, together = buy(db, splitter, event, settings, 2), buy(db, bundler, event, settings, 2)

    create_refund(db, splitter, split.id, refund_of(split.items[0].id, 1), settings)
    create_refund(db, splitter, split.id, refund_of(split.items[0].id, 1), settings)
    create_refund(db, bundler, together.id, refund_of(together.items[0].id, 2), settings)

    db.refresh(split)
    db.refresh(together)
    assert split.refunded_amount == together.refunded_amount == Decimal("25.26")  # 2 x 12.63


# --- Smaller fixes ----------------------------------------------------------------


def test_a_reservation_cannot_be_confirmed_after_the_event_starts(db, user, make_event, settings):
    event = make_event(starts_in=timedelta(minutes=5))
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 1)), settings)

    with pytest.raises(SalesClosedError):
        confirm_purchase(db, user, purchase.id, now=datetime.now(UTC) + timedelta(minutes=6))
    assert counters(general.id) == (1, 0)  # still held; the sweeper releases it


def test_ticket_type_names_are_unique_ignoring_case(client, make_user, make_event):
    event = make_event()
    admin = auth_headers(make_user(is_admin=True))
    payload = {"name": "vip", "price": "10.00", "capacity": 5}

    response = client.post(f"/events/{event.id}/ticket-types", json=payload, headers=admin)

    assert response.status_code == 409


def test_events_cannot_be_created_in_the_past(client, make_user):
    payload = {
        "name": "Yesterday",
        "venue": "Venue",
        "starts_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "ticket_types": [{"name": "General", "price": "10.00", "capacity": 5}],
    }

    response = client.post("/events", json=payload, headers=auth_headers(make_user(is_admin=True)))

    assert response.status_code == 422


def test_simultaneous_registrations_of_one_email_never_fail_with_500(client):
    barrier = Barrier(6)
    payload = {"email": "race@example.com", "full_name": "Racer", "password": "a-long-password"}

    def register(_: int) -> int:
        barrier.wait()
        return client.post("/auth/register", json=payload).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = sorted(pool.map(register, range(6)))

    assert statuses == [201, 409, 409, 409, 409, 409]


def test_login_ignores_case_and_surrounding_spaces_in_the_email(client, make_user):
    make_user(email="someone@example.com")

    response = client.post("/auth/login", data={"username": "  Someone@Example.com ", "password": "test-password-123"})

    assert response.status_code == 200


def test_event_list_hides_past_events_and_is_paginated(client, make_event):
    make_event(starts_in=timedelta(days=-1))
    upcoming = [make_event(starts_in=timedelta(days=d)) for d in (1, 2, 3)]

    default = client.get("/events").json()
    page = client.get("/events", params={"limit": 2, "offset": 1}).json()
    everything = client.get("/events", params={"include_past": True}).json()

    assert [e["id"] for e in default] == [e.id for e in upcoming]
    assert [e["id"] for e in page] == [e.id for e in upcoming[1:]]
    assert len(everything) == 4
    assert client.get("/events", params={"limit": 1000}).status_code == 422


def test_purchase_list_is_paginated(client, db, user, make_event, settings):
    event = make_event()
    for _ in range(3):
        buy(db, user, event, settings, 1)

    page = client.get("/purchases", params={"limit": 2}, headers=auth_headers(user)).json()

    assert len(page) == 2


def test_only_admins_see_the_sales_breakdown(client, db, make_user, make_event, settings):
    event = make_event()
    customer, admin = make_user(), make_user(is_admin=True)
    buy(db, customer, event, settings, 2)

    def general(headers: dict[str, str] | None) -> dict:
        ticket_types = client.get(f"/events/{event.id}", headers=headers).json()["ticket_types"]
        return next(tt for tt in ticket_types if tt["name"] == "General")

    for headers in (None, auth_headers(customer), {"Authorization": "Bearer not-a-token"}):
        seen = general(headers)
        assert (seen["available"], seen["held"], seen["sold"]) == (98, None, None)
    assert general(auth_headers(admin))["sold"] == 2
    assert client.get("/events", headers=auth_headers(admin)).json()[0]["ticket_types"][0]["sold"] == 2


def test_health_checks_the_database_and_responses_carry_a_request_id(client):
    response = client.get("/health")

    assert response.json() == {"status": "ok"}
    assert len(response.headers["X-Request-ID"]) == 32
    assert client.get("/health").headers["X-Request-ID"] != response.headers["X-Request-ID"]
