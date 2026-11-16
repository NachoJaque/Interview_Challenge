"""Load test: N buyers compete at the same instant for 50 tickets.

    python scripts/load_test.py [N]          (default 200; needs the stack running)

It talks to the API directly on port 8000, so nginx's rate limits do not
interfere, and reads the admin account from .env. It creates its own event
and N accounts named load-<run>-<n>@example.com, so use a disposable database.

What to look for: winners == tickets, held + sold never above capacity, and
no 5xx other than an honest 503 "service_busy" when the site is over capacity.
"""

import json
import pathlib
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

B = "http://127.0.0.1:8000"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 200
CAPACITY = 50
env = dict(
    line.split("=", 1) for line in pathlib.Path(".env").read_text().splitlines() if "=" in line and not line.startswith("#")
)


def call(method, path, token=None, body=None, form=None, timeout=90):
    headers, data = {}, None
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(B + path, data=data, method=method, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except Exception as exc:  # timeouts, resets
        return 0, {"error": {"code": type(exc).__name__}}, time.perf_counter() - started
    try:
        payload = json.loads(raw or b"null")
    except ValueError:
        payload = None
    return status, payload, time.perf_counter() - started


def summary(label, results):
    latencies = sorted(r[2] for r in results)
    outcomes = Counter(
        f"{status} {payload['error']['code']}" if isinstance(payload, dict) and "error" in payload else str(status)
        for status, payload, _ in results
    )
    pct = lambda q: latencies[min(len(latencies) - 1, int(q * len(latencies)))]  # noqa: E731
    print(f"{label}: {dict(outcomes)}")
    print(f"   latency s: p50 {statistics.median(latencies):.2f}  p95 {pct(0.95):.2f}  max {latencies[-1]:.2f}")


def simultaneously(tasks):
    barrier = threading.Barrier(len(tasks))

    def run(task):
        barrier.wait()
        return task()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        results = list(pool.map(run, tasks))
    return results, time.perf_counter() - started


status, payload, _ = call("POST", "/auth/login", form={"username": env["ADMIN_EMAIL"], "password": env["ADMIN_PASSWORD"]})
assert status == 200, (status, payload)
admin = payload["access_token"]
status, event, _ = call(
    "POST",
    "/events",
    admin,
    {
        "name": f"Load test {uuid.uuid4().hex[:6]}",
        "venue": "Lab",
        "starts_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "ticket_types": [{"name": "General", "price": "10.00", "capacity": CAPACITY}],
    },
)
assert status == 201, (status, event)
general = event["ticket_types"][0]["id"]
print(f"event {event['id']} with {CAPACITY} tickets; {N} buyers")

# 1. Sign-up burst (password hashing is the cost here)
run_id = uuid.uuid4().hex[:8]
accounts = [(f"load-{run_id}-{i}@example.com", f"pw-{run_id}-{i}") for i in range(N)]
results, took = simultaneously(
    [lambda a=a: call("POST", "/auth/register", body={"email": a[0], "full_name": "Load", "password": a[1]}) for a in accounts]
)
summary(f"1. {N} sign-ups at once ({N / took:.0f}/s)", results)

results, took = simultaneously(
    [lambda a=a: call("POST", "/auth/login", form={"username": a[0], "password": a[1]}) for a in accounts]
)
summary(f"2. {N} logins at once ({N / took:.0f}/s)", results)
tokens = [payload["access_token"] for status, payload, _ in results if status == 200]

# 3. Everyone goes for the tickets at the same instant
body = {"items": [{"ticket_type_id": general, "quantity": 1}]}
results, took = simultaneously([lambda t=t: call("POST", f"/events/{event['id']}/purchases", t, body) for t in tokens])
summary(f"3. {len(tokens)} reservations at once for {CAPACITY} tickets ({len(tokens) / took:.0f}/s)", results)
winners = [(t, payload["id"]) for t, (status, payload, _) in zip(tokens, results) if status == 201]
time.sleep(1.5)  # event data may be served from a one-second cache
state = call("GET", f"/events/{event['id']}", admin)[1]["ticket_types"][0]
print(f"   winners {len(winners)} | held {state['held']} sold {state['sold']} available {state['available']}")

results, took = simultaneously([lambda w=w: call("POST", f"/purchases/{w[1]}/confirm", w[0]) for w in winners])
summary(f"4. {len(winners)} confirmations at once ({len(winners) / took:.0f}/s)", results)
time.sleep(1.5)  # event data may be served from a one-second cache
state = call("GET", f"/events/{event['id']}", admin)[1]["ticket_types"][0]
print(f"   held {state['held']} sold {state['sold']} available {state['available']}")

# 5. Sold-out stampede: everyone who lost retries at once
losers = [t for t in tokens if t not in {w[0] for w in winners}] or tokens[:1]
results, took = simultaneously([lambda t=t: call("POST", f"/events/{event['id']}/purchases", t, body) for t in losers])
summary(f"5. {len(losers)} retries on a sold-out event ({len(losers) / took:.0f}/s)", results)

# 6. What the pages' polling costs: every browser asks for these every 3 seconds
for label, path, token in (("events list (anonymous)", "/events", None), ("my purchases (logged in)", "/purchases", tokens[0])):
    stop = time.perf_counter() + 8
    collected = []

    def poll(path=path, token=token, stop=stop, collected=collected):
        while time.perf_counter() < stop:
            collected.append(call("GET", path, token))

    workers = [threading.Thread(target=poll) for _ in range(64)]
    [w.start() for w in workers]
    [w.join() for w in workers]
    summary(f"6. polling {label}, 64 clients non-stop: {len(collected) / 8:.0f} req/s", collected)
