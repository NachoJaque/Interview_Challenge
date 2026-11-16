from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

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
from app.models import PurchaseStatus
from app.services.purchases import cancel_purchase, confirm_purchase, release_expired_purchases, reserve_tickets
from app.services.refunds import create_refund
from tests.conftest import counters, refund_of, request_for, ticket_type

# --- Reserving ---------------------------------------------------------------


def test_reserving_holds_tickets_without_selling_them(db, user, make_event, settings):
    event = make_event()
    general, vip = ticket_type(event, "General"), ticket_type(event, "VIP")

    purchase, created = reserve_tickets(db, user, event.id, request_for((general.id, 2), (vip.id, 1)), settings)

    assert created
    assert purchase.status == PurchaseStatus.PENDING
    assert purchase.total_amount == Decimal("250.00")  # 2 x 50 + 1 x 150
    assert purchase.expires_at > datetime.now(UTC)
    assert counters(general.id) == (2, 0)
    assert counters(vip.id) == (1, 0)


def test_reservation_snapshots_the_unit_price(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 1)), settings)

    general.price = Decimal("99.00")
    db.commit()
    db.refresh(purchase)

    assert purchase.items[0].unit_price == Decimal("50.00")


def test_cannot_reserve_more_than_available(db, user, make_event, settings):
    event = make_event(vip_capacity=2)
    vip = ticket_type(event, "VIP")

    with pytest.raises(InsufficientCapacityError):
        reserve_tickets(db, user, event.id, request_for((vip.id, 3)), settings)

    assert counters(vip.id) == (0, 0)


def test_reservation_is_all_or_nothing_across_ticket_types(db, user, make_event, settings):
    event = make_event(general_capacity=100, vip_capacity=1)
    general, vip = ticket_type(event, "General"), ticket_type(event, "VIP")

    with pytest.raises(InsufficientCapacityError):
        reserve_tickets(db, user, event.id, request_for((general.id, 2), (vip.id, 2)), settings)

    # The General tickets that were held first must have been rolled back.
    assert counters(general.id) == (0, 0)
    assert counters(vip.id) == (0, 0)


def test_held_tickets_are_not_available_to_other_users(db, make_user, make_event, settings):
    event = make_event(vip_capacity=2)
    vip = ticket_type(event, "VIP")
    reserve_tickets(db, make_user(), event.id, request_for((vip.id, 2)), settings)

    with pytest.raises(InsufficientCapacityError):
        reserve_tickets(db, make_user(), event.id, request_for((vip.id, 1)), settings)


def test_cannot_reserve_for_an_event_that_already_started(db, user, make_event, settings):
    event = make_event(starts_in=timedelta(hours=-1))

    with pytest.raises(SalesClosedError):
        reserve_tickets(db, user, event.id, request_for((ticket_type(event, "General").id, 1)), settings)


def test_cannot_exceed_the_per_purchase_limit(db, user, make_event, settings):
    event = make_event()
    general, vip = ticket_type(event, "General"), ticket_type(event, "VIP")

    with pytest.raises(InvalidPurchaseError):
        reserve_tickets(db, user, event.id, request_for((general.id, 8), (vip.id, 3)), settings)


def test_ticket_type_must_belong_to_the_event(db, user, make_event, settings):
    event, other_event = make_event(), make_event()

    with pytest.raises(NotFoundError):
        reserve_tickets(db, user, event.id, request_for((ticket_type(other_event, "VIP").id, 1)), settings)


def test_ticket_type_cannot_be_repeated_in_one_purchase(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")

    with pytest.raises(InvalidPurchaseError):
        reserve_tickets(db, user, event.id, request_for((general.id, 1), (general.id, 1)), settings)


def test_unknown_event(db, user, settings):
    with pytest.raises(NotFoundError):
        reserve_tickets(db, user, 999, request_for((1, 1)), settings)


def test_only_one_pending_reservation_per_user_and_event(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    reserve_tickets(db, user, event.id, request_for((general.id, 1)), settings)

    with pytest.raises(PendingReservationExistsError):
        reserve_tickets(db, user, event.id, request_for((general.id, 1)), settings)

    assert counters(general.id) == (1, 0)


# --- Idempotency -------------------------------------------------------------


def test_retrying_with_the_same_idempotency_key_returns_the_same_reservation(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    request = request_for((general.id, 2))

    first, created_first = reserve_tickets(db, user, event.id, request, settings, idempotency_key="retry-key-1")
    second, created_second = reserve_tickets(db, user, event.id, request, settings, idempotency_key="retry-key-1")

    assert created_first and not created_second
    assert second.id == first.id
    assert counters(general.id) == (2, 0)  # held once, not twice


def test_idempotency_key_cannot_be_reused_for_another_event(db, user, make_event, settings):
    event, other_event = make_event(), make_event()
    reserve_tickets(
        db, user, event.id, request_for((ticket_type(event, "General").id, 1)), settings, idempotency_key="retry-key-2"
    )

    with pytest.raises(ConflictError):
        reserve_tickets(
            db,
            user,
            other_event.id,
            request_for((ticket_type(other_event, "General").id, 1)),
            settings,
            idempotency_key="retry-key-2",
        )


# --- Confirming --------------------------------------------------------------


def test_confirming_turns_held_tickets_into_sold_tickets(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 3)), settings)

    confirmed = confirm_purchase(db, user, purchase.id)

    assert confirmed.status == PurchaseStatus.COMPLETED
    assert confirmed.confirmed_at is not None
    assert counters(general.id) == (0, 3)


def test_confirming_twice_does_not_sell_twice(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 3)), settings)

    confirm_purchase(db, user, purchase.id)
    confirm_purchase(db, user, purchase.id)

    assert counters(general.id) == (0, 3)


def test_cannot_confirm_someone_elses_reservation(db, make_user, make_event, settings):
    owner, intruder = make_user(), make_user()
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, owner, event.id, request_for((general.id, 1)), settings)

    with pytest.raises(NotFoundError):
        confirm_purchase(db, intruder, purchase.id)

    assert counters(general.id) == (1, 0)


def test_confirming_an_expired_reservation_fails_and_releases_the_tickets(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 2)), settings)
    after_expiry = purchase.expires_at + timedelta(seconds=1)

    with pytest.raises(ReservationExpiredError):
        confirm_purchase(db, user, purchase.id, now=after_expiry)

    db.refresh(purchase)
    assert purchase.status == PurchaseStatus.EXPIRED
    assert counters(general.id) == (0, 0)


# --- Cancelling and expiring -------------------------------------------------


def test_cancelling_releases_the_held_tickets(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 2)), settings)

    cancelled = cancel_purchase(db, user, purchase.id)

    assert cancelled.status == PurchaseStatus.CANCELLED
    assert counters(general.id) == (0, 0)


def test_cannot_cancel_a_confirmed_purchase(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 2)), settings)
    confirm_purchase(db, user, purchase.id)

    with pytest.raises(InvalidPurchaseStateError):
        cancel_purchase(db, user, purchase.id)

    assert counters(general.id) == (0, 2)


def test_cannot_confirm_a_cancelled_reservation(db, user, make_event, settings):
    event = make_event()
    purchase, _ = reserve_tickets(db, user, event.id, request_for((ticket_type(event, "General").id, 1)), settings)
    cancel_purchase(db, user, purchase.id)

    with pytest.raises(ReservationCancelledError):
        confirm_purchase(db, user, purchase.id)


def test_sweeper_releases_only_expired_reservations(db, make_user, make_event, settings):
    # Two events, because reserving already reclaims stale holds of its own event.
    stale_event, fresh_event = make_event(), make_event()
    stale_general, fresh_general = ticket_type(stale_event, "General"), ticket_type(fresh_event, "General")
    now = datetime.now(UTC)
    stale, _ = reserve_tickets(
        db, make_user(), stale_event.id, request_for((stale_general.id, 2)), settings, now=now - timedelta(minutes=30)
    )
    fresh, _ = reserve_tickets(db, make_user(), fresh_event.id, request_for((fresh_general.id, 3)), settings, now=now)

    released = release_expired_purchases(db, now)

    db.refresh(stale)
    db.refresh(fresh)
    assert released == 1
    assert stale.status == PurchaseStatus.EXPIRED
    assert fresh.status == PurchaseStatus.PENDING
    assert counters(stale_general.id) == (0, 0)
    assert counters(fresh_general.id) == (3, 0)


def test_sweeper_never_touches_confirmed_purchases(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 2)), settings)
    confirm_purchase(db, user, purchase.id)

    released = release_expired_purchases(db, purchase.expires_at + timedelta(hours=1))

    assert released == 0
    assert counters(general.id) == (0, 2)


def test_expired_holds_are_reclaimed_when_someone_else_reserves(db, make_user, make_event, settings):
    """Availability must not depend on the background sweeper having run."""
    event = make_event(vip_capacity=2)
    vip = ticket_type(event, "VIP")
    now = datetime.now(UTC)
    reserve_tickets(db, make_user(), event.id, request_for((vip.id, 2)), settings, now=now - timedelta(minutes=30))

    purchase, created = reserve_tickets(db, make_user(), event.id, request_for((vip.id, 2)), settings, now=now)

    assert created
    assert purchase.status == PurchaseStatus.PENDING
    assert counters(vip.id) == (2, 0)


def test_user_can_reserve_again_after_their_reservation_expired(db, user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    now = datetime.now(UTC)
    reserve_tickets(db, user, event.id, request_for((general.id, 1)), settings, now=now - timedelta(minutes=30))

    purchase, created = reserve_tickets(db, user, event.id, request_for((general.id, 4)), settings, now=now)

    assert created
    assert counters(general.id) == (4, 0)


# --- Limits per customer ------------------------------------------------------


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


def test_completed_purchases_do_not_count_as_abandoned(db, user, make_event, settings, buy):
    event = make_event()
    for _ in range(settings.max_abandoned_reservations + 1):
        buy(user, event, General=1)

    assert counters(ticket_type(event, "General").id) == (0, settings.max_abandoned_reservations + 1)


def test_a_customer_cannot_exceed_the_per_event_limit_across_purchases(db, user, make_event, settings, buy):
    event = make_event()
    general = ticket_type(event, "General")
    buy(user, event, General=10)
    buy(user, event, General=10)

    with pytest.raises(TicketLimitExceededError):
        reserve_tickets(db, user, event.id, request_for((general.id, 1)), settings)
    assert counters(general.id) == (0, 20)


def test_refunded_tickets_free_room_under_the_limit(db, user, make_event, settings, buy):
    event = make_event()
    general = ticket_type(event, "General")
    buy(user, event, General=10)
    purchase = buy(user, event, General=10)
    create_refund(db, user, purchase.id, refund_of((purchase.items[0].id, 4)), settings)

    with pytest.raises(TicketLimitExceededError):
        reserve_tickets(db, user, event.id, request_for((general.id, 5)), settings)
    assert buy(user, event, General=4).status == PurchaseStatus.COMPLETED


def test_the_limit_is_per_customer(db, make_user, make_event, settings, buy):
    event = make_event()
    first, second = make_user(), make_user()
    buy(first, event, General=10)
    buy(first, event, General=10)

    assert buy(second, event, General=10).status == PurchaseStatus.COMPLETED


def test_a_reservation_cannot_be_confirmed_after_the_event_starts(db, user, make_event, settings):
    event = make_event(starts_in=timedelta(minutes=5))
    general = ticket_type(event, "General")
    purchase, _ = reserve_tickets(db, user, event.id, request_for((general.id, 1)), settings)

    with pytest.raises(SalesClosedError):
        confirm_purchase(db, user, purchase.id, now=datetime.now(UTC) + timedelta(minutes=6))
    assert counters(general.id) == (1, 0)  # still held; the sweeper releases it


def test_a_purchase_request_releases_only_a_few_expired_reservations(db, make_user, make_event, settings):
    event = make_event()
    general = ticket_type(event, "General")
    backlog = settings.reservation_lazy_release_limit + 3
    for _ in range(backlog):
        reserve_tickets(db, make_user(), event.id, request_for((general.id, 1)), settings)
    after_expiry = datetime.now(UTC) + timedelta(minutes=settings.reservation_ttl_minutes + 1)

    reserve_tickets(db, make_user(), event.id, request_for((general.id, 1)), settings, now=after_expiry)

    # The newcomer holds 1; it released `limit` of the backlog and left 3 for the sweeper.
    assert counters(general.id) == (3 + 1, 0)
    assert release_expired_purchases(db, after_expiry) == 3
    assert counters(general.id) == (1, 0)
