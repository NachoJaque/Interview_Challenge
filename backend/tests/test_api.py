"""End-to-end tests through HTTP: routing, authentication, status codes,
response shapes and the security measures."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import jwt
from sqlalchemy import select
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.config import get_settings
from app.database import engine
from app.models import User
from tests.conftest import TEST_PASSWORD, auth_headers, counters, ticket_type

# --- Accounts -----------------------------------------------------------------


def test_register_login_and_read_profile(client):
    registered = client.post(
        "/auth/register",
        json={"email": "Ana@Example.com", "full_name": "Ana", "password": "a-strong-password"},
    )
    assert registered.status_code == 201
    assert registered.json()["email"] == "ana@example.com"
    assert "password" not in registered.text

    login = client.post("/auth/login", data={"username": "ana@example.com", "password": "a-strong-password"})
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["is_admin"] is False


def test_passwords_are_stored_hashed(client, db):
    client.post(
        "/auth/register", json={"email": "ana@example.com", "full_name": "Ana", "password": "a-strong-password"}
    )

    stored = db.scalar(select(User.hashed_password).where(User.email == "ana@example.com"))

    assert stored != "a-strong-password"
    assert stored.startswith("$2")  # bcrypt


def test_duplicate_email_is_rejected(client, make_user):
    make_user(email="taken@example.com")

    response = client.post(
        "/auth/register", json={"email": "TAKEN@example.com", "full_name": "Other", "password": "a-strong-password"}
    )

    assert response.status_code == 409


def test_weak_password_is_rejected(client):
    response = client.post("/auth/register", json={"email": "ana@example.com", "full_name": "Ana", "password": "short"})

    assert response.status_code == 422


def test_login_failure_does_not_reveal_whether_the_account_exists(client, make_user):
    make_user(email="real@example.com")

    wrong_password = client.post("/auth/login", data={"username": "real@example.com", "password": "wrong-password"})
    unknown_email = client.post("/auth/login", data={"username": "ghost@example.com", "password": "wrong-password"})

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_registration_cannot_grant_admin(client, db):
    """Mass assignment: unknown fields such as is_admin are rejected outright."""
    response = client.post(
        "/auth/register",
        json={"email": "eve@example.com", "full_name": "Eve", "password": "a-strong-password", "is_admin": True},
    )

    assert response.status_code == 422
    assert db.scalar(select(User).where(User.email == "eve@example.com")) is None


# --- Authentication and authorization ----------------------------------------


def test_protected_endpoints_require_a_token(client, make_event):
    event = make_event()

    assert client.get("/purchases").status_code == 401
    assert client.post(f"/events/{event.id}/purchases", json={"items": []}).status_code == 401
    assert client.post("/purchases/1/confirm").status_code == 401
    assert client.post("/purchases/1/refunds", json={"items": []}).status_code == 401


def test_forged_or_expired_tokens_are_rejected(client, user):
    forged = jwt.encode({"sub": str(user.id)}, "not-the-real-secret-but-long-enough-key", "HS256")
    unsigned = jwt.encode({"sub": str(user.id)}, "", "none")
    settings = get_settings()
    expired = jwt.encode(
        {"sub": str(user.id), "exp": datetime.now(UTC) - timedelta(minutes=1)},
        settings.jwt_secret,
        settings.jwt_algorithm,
    )

    for token in (forged, unsigned, expired, "garbage"):
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_only_admins_can_create_events(client, make_user):
    payload = {
        "name": "New Event",
        "venue": "Somewhere",
        "starts_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        "ticket_types": [
            {"name": "General", "price": "40.00", "capacity": 100},
            {"name": "VIP", "price": "120.00", "capacity": 10},
        ],
    }

    assert client.post("/events", json=payload).status_code == 401
    assert client.post("/events", json=payload, headers=auth_headers(make_user())).status_code == 403

    created = client.post("/events", json=payload, headers=auth_headers(make_user(is_admin=True)))
    assert created.status_code == 201
    assert [tt["name"] for tt in created.json()["ticket_types"]] == ["General", "VIP"]


def test_admin_can_add_a_new_ticket_type_without_any_code_change(client, make_user, make_event):
    event = make_event()
    admin = auth_headers(make_user(is_admin=True))

    added = client.post(
        f"/events/{event.id}/ticket-types",
        json={"name": "Platinum", "price": "400.00", "capacity": 5},
        headers=admin,
    )
    assert added.status_code == 201

    # ...and it is immediately purchasable through the same endpoint.
    buyer = auth_headers(make_user())
    reserved = client.post(
        f"/events/{event.id}/purchases",
        json={"items": [{"ticket_type_id": added.json()["id"], "quantity": 2}]},
        headers=buyer,
    )
    assert reserved.status_code == 201
    assert reserved.json()["total_amount"] == "800.00"

    duplicate = client.post(
        f"/events/{event.id}/ticket-types", json={"name": "Platinum", "price": "1.00", "capacity": 1}, headers=admin
    )
    assert duplicate.status_code == 409


def test_users_cannot_see_or_touch_each_others_purchases(client, make_user, make_event):
    owner, intruder = auth_headers(make_user()), auth_headers(make_user())
    event = make_event()
    general = ticket_type(event, "General")
    purchase = client.post(
        f"/events/{event.id}/purchases",
        json={"items": [{"ticket_type_id": general.id, "quantity": 1}]},
        headers=owner,
    ).json()

    assert client.get(f"/purchases/{purchase['id']}", headers=intruder).status_code == 404
    assert client.post(f"/purchases/{purchase['id']}/confirm", headers=intruder).status_code == 404
    assert client.post(f"/purchases/{purchase['id']}/cancel", headers=intruder).status_code == 404
    assert client.get("/purchases", headers=intruder).json() == []
    assert counters(general.id) == (1, 0)


# --- The full purchase and refund flow ----------------------------------------


def test_reserve_confirm_and_partially_refund(client, user, make_event):
    headers = auth_headers(user)
    event = make_event(starts_in=timedelta(days=30))
    general, vip = ticket_type(event, "General"), ticket_type(event, "VIP")

    reserved = client.post(
        f"/events/{event.id}/purchases",
        json={"items": [{"ticket_type_id": general.id, "quantity": 3}, {"ticket_type_id": vip.id, "quantity": 1}]},
        headers=headers,
    )
    assert reserved.status_code == 201
    purchase = reserved.json()
    assert purchase["status"] == "pending"
    assert purchase["total_amount"] == "300.00"
    assert purchase["refund_policy"]["allowed"] is False  # nothing to refund until confirmed

    listed = client.get(f"/events/{event.id}").json()
    assert {tt["name"]: tt["available"] for tt in listed["ticket_types"]} == {"General": 97, "VIP": 9}
    assert (counters(general.id), counters(vip.id)) == ((3, 0), (1, 0))

    confirmed = client.post(f"/purchases/{purchase['id']}/confirm", headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "completed"
    assert confirmed.json()["refund_policy"] == {
        "allowed": True,
        "percentage": 100,
        "reason": "Full refund: 7 days or more before the event.",
    }

    general_item = next(i for i in purchase["items"] if i["ticket_type_name"] == "General")
    refunded = client.post(
        f"/purchases/{purchase['id']}/refunds",
        json={"items": [{"purchase_item_id": general_item["id"], "quantity": 2}], "reason": "Friends cancelled"},
        headers=headers,
    )
    assert refunded.status_code == 201
    body = refunded.json()
    assert body["status"] == "partially_refunded"
    assert body["refunded_amount"] == "100.00"
    assert body["refunds"][0]["refund_percentage"] == 100
    assert next(i for i in body["items"] if i["id"] == general_item["id"])["refundable_quantity"] == 1

    assert counters(general.id) == (0, 1)
    assert counters(vip.id) == (0, 1)
    assert [p["id"] for p in client.get("/purchases", headers=headers).json()] == [purchase["id"]]


def test_sold_out_returns_409_with_a_machine_readable_code(client, make_user, make_event):
    event = make_event(vip_capacity=1)
    vip = ticket_type(event, "VIP")
    body = {"items": [{"ticket_type_id": vip.id, "quantity": 1}]}
    client.post(f"/events/{event.id}/purchases", json=body, headers=auth_headers(make_user()))

    response = client.post(f"/events/{event.id}/purchases", json=body, headers=auth_headers(make_user()))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_capacity"


def test_refund_outside_the_window_returns_422(client, user, make_event):
    headers = auth_headers(user)
    event = make_event(starts_in=timedelta(hours=20))
    general = ticket_type(event, "General")
    purchase = client.post(
        f"/events/{event.id}/purchases",
        json={"items": [{"ticket_type_id": general.id, "quantity": 1}]},
        headers=headers,
    ).json()
    client.post(f"/purchases/{purchase['id']}/confirm", headers=headers)

    response = client.post(
        f"/purchases/{purchase['id']}/refunds",
        json={"items": [{"purchase_item_id": purchase["items"][0]["id"], "quantity": 1}]},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "refund_not_allowed"


def test_idempotency_key_replay_returns_200_and_the_same_purchase(client, user, make_event):
    headers = {**auth_headers(user), "Idempotency-Key": "checkout-attempt-1"}
    event = make_event()
    general = ticket_type(event, "General")
    body = {"items": [{"ticket_type_id": general.id, "quantity": 2}]}

    first = client.post(f"/events/{event.id}/purchases", json=body, headers=headers)
    replay = client.post(f"/events/{event.id}/purchases", json=body, headers=headers)

    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.json()["id"] == first.json()["id"]
    assert counters(general.id) == (2, 0)


def test_cancel_releases_the_reservation(client, user, make_event):
    headers = auth_headers(user)
    event = make_event()
    general = ticket_type(event, "General")
    purchase = client.post(
        f"/events/{event.id}/purchases",
        json={"items": [{"ticket_type_id": general.id, "quantity": 4}]},
        headers=headers,
    ).json()

    cancelled = client.post(f"/purchases/{purchase['id']}/cancel", headers=headers)

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert counters(general.id) == (0, 0)


# --- Input validation and injection -------------------------------------------


def test_invalid_quantities_are_rejected(client, user, make_event):
    headers = auth_headers(user)
    event = make_event()
    general = ticket_type(event, "General")

    for quantity in (0, -3, 1.5, "many", 10**12):
        response = client.post(
            f"/events/{event.id}/purchases",
            json={"items": [{"ticket_type_id": general.id, "quantity": quantity}]},
            headers=headers,
        )
        assert response.status_code == 422, quantity

    assert client.post(f"/events/{event.id}/purchases", json={"items": []}, headers=headers).status_code == 422
    assert counters(general.id) == (0, 0)


def test_clients_cannot_set_the_price_or_status(client, user, make_event):
    event = make_event()
    general = ticket_type(event, "General")

    response = client.post(
        f"/events/{event.id}/purchases",
        json={
            "items": [{"ticket_type_id": general.id, "quantity": 1, "unit_price": "0.01"}],
            "status": "completed",
        },
        headers=auth_headers(user),
    )

    assert response.status_code == 422
    assert counters(general.id) == (0, 0)


def test_out_of_range_ids_are_rejected_before_reaching_the_database(client, user):
    headers = auth_headers(user)

    assert client.get("/events/99999999999999999999").status_code == 422
    assert client.get("/events/0").status_code == 422
    assert client.get("/events/1 OR 1=1").status_code == 422
    assert client.get("/purchases/99999999999999999999", headers=headers).status_code == 422


def test_sql_injection_attempts_are_treated_as_plain_data(client, db, make_user, make_event):
    make_user(email="victim@example.com")
    make_event()

    # Classic authentication bypass: must simply fail to log in.
    for username in ("' OR '1'='1' --", "victim@example.com' --", "admin'; DROP TABLE users; --"):
        response = client.post("/auth/login", data={"username": username, "password": "' OR '1'='1"})
        assert response.status_code == 401

    # A hostile string in a stored field is saved and returned verbatim.
    hostile = "Robert'); DROP TABLE events; --"
    created = client.post(
        "/events",
        json={
            "name": hostile,
            "venue": "<script>alert(1)</script>",
            "starts_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
            "ticket_types": [{"name": "General", "price": "10.00", "capacity": 5}],
        },
        headers=auth_headers(make_user(is_admin=True)),
    )
    assert created.status_code == 201
    assert created.json()["name"] == hostile

    # Every table is still there and intact.
    assert db.scalar(select(User).where(User.email == "victim@example.com")) is not None
    assert len(client.get("/events").json()) == 2


def test_login_works_with_the_shared_test_password(client, make_user):
    make_user(email="someone@example.com")

    response = client.post("/auth/login", data={"username": "someone@example.com", "password": TEST_PASSWORD})

    assert response.status_code == 200


# --- HTTP hardening -----------------------------------------------------------


def test_security_headers_are_present_on_every_response(client):
    for response in (client.get("/health"), client.get("/events"), client.get("/purchases")):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["Cache-Control"] == "no-store"


def test_cors_only_allows_configured_origins(client):
    allowed = client.options(
        "/events", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"}
    )
    blocked = client.options(
        "/events", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )

    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in blocked.headers


# --- Lists, visibility and operations --------------------------------------------


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


def test_purchase_list_is_paginated(client, db, user, make_event, settings, buy):
    event = make_event()
    for _ in range(3):
        buy(user, event, General=1)

    page = client.get("/purchases", params={"limit": 2}, headers=auth_headers(user)).json()

    assert len(page) == 2


def test_only_admins_see_the_sales_breakdown(client, db, make_user, make_event, settings, buy):
    event = make_event()
    customer, admin = make_user(), make_user(is_admin=True)
    buy(customer, event, General=2)

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


# --- One error format --------------------------------------------------------------


def test_every_kind_of_error_has_the_same_shape(client, user, make_user):
    make_user(email="taken@example.com")
    account = {"email": "taken@example.com", "full_name": "Someone", "password": "a-long-password"}
    failures = {
        "not_authenticated": client.get("/purchases"),
        "invalid_credentials": client.post("/auth/login", data={"username": "nobody@example.com", "password": "x"}),
        "forbidden": client.post("/events", json={}, headers=auth_headers(user)),
        "email_taken": client.post("/auth/register", json=account),
        "not_found": client.get("/events/999"),
        "validation_error": client.post("/auth/register", json={**account, "password": "short"}),
        "method_not_allowed": client.delete("/events"),
    }

    for code, response in failures.items():
        error = response.json()["error"]
        assert error["code"] == code, response.text
        assert isinstance(error["message"], str) and error["message"]
    assert [failures[code].status_code for code in failures] == [401, 401, 403, 409, 404, 422, 405]


def test_authentication_errors_keep_the_bearer_challenge(client):
    for response in (client.get("/purchases"), client.get("/purchases", headers={"Authorization": "Bearer nope"})):
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_validation_errors_name_the_field_and_never_echo_the_input(client):
    secret = "pw-1234"
    response = client.post("/auth/register", json={"email": "a@example.com", "full_name": "A", "password": secret})

    error = response.json()["error"]
    assert error["message"].startswith("password: ")
    assert error["details"][0]["field"] == "password"
    assert secret not in response.text


def test_confirming_a_cancelled_reservation_reports_it_as_cancelled(client, user, make_event):
    headers = auth_headers(user)
    event = make_event()
    body = {"items": [{"ticket_type_id": ticket_type(event, "General").id, "quantity": 1}]}
    purchase = client.post(f"/events/{event.id}/purchases", json=body, headers=headers).json()
    client.post(f"/purchases/{purchase['id']}/cancel", headers=headers)

    response = client.post(f"/purchases/{purchase['id']}/confirm", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reservation_cancelled"


def test_purchase_rules_are_public_and_match_the_settings(client, settings):
    response = client.get("/purchase-rules")

    assert response.status_code == 200
    assert response.json() == {
        "max_tickets_per_purchase": settings.max_tickets_per_purchase,
        "max_tickets_per_user_per_event": settings.max_tickets_per_user_per_event,
        "reservation_ttl_minutes": settings.reservation_ttl_minutes,
    }


# --- Behaviour under load ------------------------------------------------------------


def test_a_saturated_database_answers_503_with_a_retry_hint(client, user, make_event, monkeypatch):
    def no_free_connection(*args, **kwargs):
        raise PoolTimeoutError("QueuePool limit reached")

    monkeypatch.setattr("app.routers.purchases.purchase_service.reserve_tickets", no_free_connection)
    event = make_event()
    body = {"items": [{"ticket_type_id": ticket_type(event, "General").id, "quantity": 1}]}

    response = client.post(f"/events/{event.id}/purchases", json=body, headers=auth_headers(user))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_busy"
    assert response.headers["Retry-After"] == "2"


def test_every_request_gives_its_connection_back(client, user, make_event):
    event = make_event()
    body = {"items": [{"ticket_type_id": ticket_type(event, "General").id, "quantity": 1}]}

    client.get("/events")
    client.get("/purchases", headers=auth_headers(user))
    client.post(f"/events/{event.id}/purchases", json=body, headers=auth_headers(user))
    client.post("/auth/login", data={"username": user.email, "password": "wrong-password"})
    client.get("/events/999")

    assert engine.pool.checkedout() == 0
