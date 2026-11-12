# Krunchbox Backend Challenge — Event Ticketing

A ticketing API for an event venue: customers **reserve**, **buy** and **refund** tickets, and the venue never sells more tickets than it has.

**Stack:** Python 3.13 · FastAPI · SQLAlchemy 2 · PostgreSQL 17 · Alembic · Docker, plus a small React + Vite web app to use it.

## Run it

Requires Docker.

```bash
cp .env.example .env
docker compose up --build
```

On start-up the API applies the migrations and loads demo data.

- **Web app:** http://localhost:8080
- **Interactive API docs:** http://localhost:8080/api/docs
- Demo accounts (a customer and an admin): see `DEMO_*` and `ADMIN_*` in `.env`

In the docs page, click **Authorize**, enter a demo account's email as `username` plus its password, and every endpoint becomes callable from the browser.

### Suggested walkthrough

1. Open the web app and log in with the demo customer. Availability on every page refreshes by itself every 3 seconds.
2. Open **Summer Rock Festival**, pick tickets and press **Reserve tickets**. The tickets are now held and a countdown starts.
3. Open a second browser tab and sign up as another customer. Sessions are per tab, so both stay logged in. The second customer sees the first one's tickets as reserved.
4. Back in the first tab, **Confirm purchase**, then go to **My purchases** and refund some of the tickets. The other tab shows them back on sale.
5. Try the same on **Jazz Night** (50% refund), **Stand-up Comedy Special** (refunds closed) and **Indie Showcase** (two tabs competing for the last tickets).
6. Log in as the admin and add a "Platinum" ticket type on any event page: it is on sale immediately, with no code change.

### Run the tests

The tests need a real PostgreSQL, because what they verify (row locks, conditional updates, constraints) does not exist in SQLite.

```bash
docker compose up -d db
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt    # Windows: .venv\Scripts\pip
.venv/bin/pytest                                 # Windows: .venv\Scripts\pytest
```

80 tests, including concurrency tests that fire simultaneous requests from real threads. They use a separate `krunchbox_test` database, created automatically.

## Where to look first

| What | File |
|------|------|
| Data models | [backend/app/models.py](backend/app/models.py) |
| Purchase and refund endpoints | [backend/app/routers/purchases.py](backend/app/routers/purchases.py) |
| Reserve / confirm / cancel / expire logic | [backend/app/services/purchases.py](backend/app/services/purchases.py) |
| Refund logic | [backend/app/services/refunds.py](backend/app/services/refunds.py) |
| Refund time windows | [backend/app/services/refund_policy.py](backend/app/services/refund_policy.py) |
| Concurrency tests | [backend/tests/test_concurrency.py](backend/tests/test_concurrency.py) |
| Web app | [frontend/src](frontend/src) |

## Assumptions

The brief leaves several things open. These are the choices made:

1. **Ticket types.** Each event sells several categories. The demo uses General and VIP, but types are rows in a table, so adding "Platinum" is one `POST /events/{id}/ticket-types` call with no code or schema change.
2. **Reservations.** Selecting tickets holds them for 10 minutes (configurable) as a `pending` purchase. If it is not confirmed in time, the tickets go back on sale.
3. **Partial refunds.** A customer can refund any number of tickets from a purchase, in one or several operations.
4. **Refund rules** apply (listed below).
5. **Accounts.** Customers register and log in; a purchase belongs to its buyer. Admins create events.
6. **Payments are out of scope.** Confirming a reservation stands in for "payment approved".
7. **General admission.** No numbered seats.

## Data model

```
User 1──N Purchase N──1 Event 1──N TicketType
              │                        │
              1                        │
              N                        │
        PurchaseItem N─────────────────┘
              │
Purchase 1──N Refund 1──N RefundItem N──1 PurchaseItem
```

| Model | Purpose | Key decision |
|-------|---------|--------------|
| `User` | Account that owns purchases | `is_admin` flag; passwords stored as bcrypt hashes |
| `Event` | Name, venue, start time | Has no capacity of its own: capacity lives on its ticket types, so two numbers can never disagree |
| `TicketType` | A sellable category for one event | Holds `capacity`, `held` and `sold`; `CHECK (held + sold <= capacity)` |
| `Purchase` | A reservation and the purchase it becomes | One row, several states; `expires_at`; idempotency key |
| `PurchaseItem` | Quantity of one ticket type in a purchase | `unit_price` snapshot; `refunded_quantity` with `CHECK (<= quantity)` |
| `Refund`, `RefundItem` | Immutable record of each refund | Stores the percentage applied, so history survives policy changes |

Purchase states:

```
pending ──confirm──▶ completed ──refund──▶ partially_refunded ──▶ refunded
   │
   ├──cancel──▶ cancelled
   └──timeout──▶ expired
```

Other choices:

- **Counters instead of counting rows.** `held` and `sold` sit on the same row as `capacity`, so availability is checked and taken in a single statement. Deriving it with `SUM` over purchases would need table-level locking or serializable isolation to be safe.
- **Reservation and purchase share a table.** Confirming is a state change, not a copy between tables.
- **Money is `NUMERIC(10,2)` / `Decimal`**, never floating point. Timestamps are timezone-aware UTC.
- **Status is `VARCHAR` + `CHECK`**, not a native PostgreSQL enum, so adding a state is an ordinary migration.

## How overselling is prevented

**Reserving** takes capacity with one conditional update per ticket type:

```sql
UPDATE ticket_types
SET held = held + :qty
WHERE id = :id AND held + sold + :qty <= capacity
RETURNING price;
```

The check and the write are the same statement. When two buyers race for the last ticket, PostgreSQL makes the second update wait for the first, then re-evaluates the `WHERE` against the new row, finds no capacity and returns nothing; the API answers `409`. Compared with `SELECT ... FOR UPDATE` followed by an update, this is one round trip and holds the lock for less time.

Around that core:

- **One transaction per operation.** A purchase with several ticket types is all-or-nothing.
- **Fixed lock order.** Ticket types are always updated in id order, and operations on an existing purchase lock the purchase row first. Two requests can therefore never deadlock.
- **Confirm, cancel and refund** start with `SELECT ... FOR UPDATE` on the purchase and re-check its state, so duplicate or simultaneous requests are processed one at a time. Refunding the same ticket twice is impossible.
- **Expiry.** A background task releases unconfirmed reservations every 10 seconds using `FOR UPDATE SKIP LOCKED`, so it is safe with several API instances and never fights a confirmation in progress. Reserving also reclaims stale holds for that event first, so availability does not depend on the task having run.
- **Database constraints as the last line of defence.** Even if application code were wrong, `CHECK (held >= 0 AND sold >= 0 AND held + sold <= capacity)` rejects an oversell.
- **Idempotency.** `POST /events/{id}/purchases` accepts an `Idempotency-Key` header; a retry returns the original reservation instead of creating a second one.

## Business rules

### Reserving and buying

1. The customer must be logged in.
2. The event must not have started.
3. Each ticket type must belong to the event and appear once per purchase.
4. At most 10 tickets per purchase.
5. There must be capacity for every requested type; otherwise nothing is reserved.
6. One pending reservation per customer and event.
7. A reservation lasts 10 minutes; unconfirmed tickets return to sale.
8. Only the owner can confirm or cancel a reservation.

### Refund rules

1. Only the owner of a purchase can refund it.
2. Only confirmed purchases can be refunded.
3. No refunds once the event has started.
4. The amount depends on how far away the event is:

   | Time before the event | Refund |
   |-----------------------|--------|
   | 7 days or more | 100% |
   | Between 48 hours and 7 days | 50% |
   | Less than 48 hours | Not allowed |

5. Ticket types marked non-refundable cannot be refunded.
6. Partial refunds are allowed: the request states a quantity per purchase item.
7. A customer cannot refund more tickets than remain un-refunded on an item.
8. The amount is based on the price paid, not the current price.
9. Refunded tickets go back on sale immediately.
10. A purchase can be refunded several times until nothing is left; its status becomes `partially_refunded` and then `refunded`.
11. If any line of a refund request is invalid, the whole request is rejected.

Limits, time windows and the percentage are settings ([backend/app/config.py](backend/app/config.py)).

## API

| Method | Path | Access | Description |
|--------|------|--------|-------------|
| POST | `/auth/register` | public | Create an account |
| POST | `/auth/login` | public | Get a JWT |
| GET | `/auth/me` | user | Current account |
| GET | `/events` | public | Events with live availability per ticket type |
| GET | `/events/{id}` | public | One event |
| POST | `/events` | admin | Create an event with its ticket types |
| POST | `/events/{id}/ticket-types` | admin | Add a ticket type to an event |
| **POST** | **`/events/{id}/purchases`** | user | **Reserve tickets** |
| **POST** | **`/purchases/{id}/confirm`** | user | **Complete the purchase** |
| POST | `/purchases/{id}/cancel` | user | Release a pending reservation |
| **POST** | **`/purchases/{id}/refunds`** | user | **Refund all or some tickets** |
| GET | `/purchases` | user | My purchases |
| GET | `/purchases/{id}` | user | One purchase, with the refund it would get right now |

Business errors share one shape, with a stable code for clients:

```json
{ "error": { "code": "insufficient_capacity", "message": "Not enough 'VIP' tickets available for 2 ticket(s)." } }
```

`401` not logged in · `403` not allowed · `404` not found · `409` sold out, expired reservation or conflict · `422` business rule or invalid input.

## Code structure

```
backend/app/
  main.py            app, CORS, security headers, error handlers, expiry task
  models.py          SQLAlchemy models
  schemas.py         request and response schemas
  routers/           thin endpoints: validate, delegate, respond
  services/          business logic, independent of HTTP
  exceptions.py      domain errors, mapped to HTTP in one place
  security.py        password hashing and JWT
  deps.py            database session, current user, admin check
  seed.py            demo data
```

```
frontend/
  src/api.ts         typed API client
  src/auth.tsx       session handling
  src/pages/         events, event (reserve and confirm), my purchases (refund), login, sign-up
  nginx.conf         serves the app, proxies /api, rate limiting and security headers
```

Endpoints contain no business logic. They call a service function, which raises domain errors; a single handler turns those into HTTP responses. The logic is therefore testable without HTTP, and each endpoint reads in a few lines.

## Security

| Risk | Measure |
|------|---------|
| SQL injection | Every query goes through SQLAlchemy with bound parameters; no SQL is built from user input |
| Malicious input | Strict request schemas: types, ranges, maximum lengths, unknown fields rejected |
| Mass assignment | Request schemas have no sensitive fields; a client cannot set `is_admin`, a price or a status |
| Accessing other users' data | Every purchase query is filtered by the token's user; someone else's purchase returns `404` |
| Passwords | bcrypt with salt; minimum length; login fails identically, and in the same time, for a wrong email or a wrong password |
| Sessions | Signed, short-lived JWT; the API refuses to start in production with the default secret |
| CSRF | The token travels in the `Authorization` header, not a cookie, so another site cannot use it |
| XSS | React escapes everything it renders; a strict `Content-Security-Policy` allows scripts and styles only from the app's own origin |
| Brute force | nginx limits login and sign-up to 20 requests per minute per IP, answering `429` beyond that |
| Token theft | The token is kept in `sessionStorage`: it is scoped to one tab and gone when the tab closes |
| Clickjacking, MIME sniffing | `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`; `Strict-Transport-Security` in production |
| Cross-origin access | CORS limited to configured origins; in Docker the app and the API share one origin through nginx |
| Information leaks | Internal errors return a generic message; account responses are never cached |
| Secrets | Read from the environment; `.env` is git-ignored |
| Transport | In production Caddy serves HTTPS only, with automatic certificates, HTTP redirected and HSTS |
| Containers | The API and nginx run as non-root users. Locally, PostgreSQL and the raw API port are published on localhost only; in production only Caddy is reachable |

The API tests cover these, including SQL-injection attempts, forged and expired tokens, and attempts to self-assign admin rights ([backend/tests/test_api.py](backend/tests/test_api.py)).

## Demo data

Event dates are relative to the moment the database is seeded, so each rule can be shown live:

| Event | Shows |
|-------|-------|
| Summer Rock Festival (in 30 days) | Full refund |
| Jazz Night (in 4 days) | 50% refund |
| Stand-up Comedy Special (in 26 hours) | Can be bought, cannot be refunded |
| Indie Showcase (5 General, 2 VIP left) | Selling out; two buyers competing for the last tickets |
| Tech Conference 2027 | A third, non-refundable ticket type ("Early Bird") |

To watch a reservation expire, set `RESERVATION_TTL_MINUTES=1` in `.env` and restart the API.

To reset the demo data: `docker compose down -v` and start again.

## Deployment

[DEPLOY.md](DEPLOY.md) is a step-by-step guide to running this on one Oracle Cloud virtual machine: the same Compose stack plus [docker-compose.prod.yml](docker-compose.prod.yml), which puts Caddy in front for HTTPS and closes every other port.

## Frontend development

To work on the web app with hot reload, keep the API running in Docker and start Vite, which proxies `/api` to it:

```bash
cd frontend
npm install
npm run dev
```

Then open http://localhost:5173.
