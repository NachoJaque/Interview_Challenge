"""The admission middleware on its own, with a stand-in application."""

import asyncio
import json

from app.admission import AdmissionMiddleware


def run_requests(limit: int, max_wait: float, paths: list[str], work_seconds: float, exempt=frozenset()):
    """Send all requests at once. Returns (statuses in request order, most
    requests ever inside the application at once, the raw responses)."""
    inside = peak = 0

    async def application(scope, receive, send):
        nonlocal inside, peak
        inside += 1
        peak = max(peak, inside)
        await asyncio.sleep(work_seconds)
        inside -= 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def request(middleware, path):
        sent = []

        async def send(message):
            sent.append(message)

        await middleware({"type": "http", "path": path}, None, send)
        return sent

    async def main():
        middleware = AdmissionMiddleware(application, limit, max_wait, exempt)
        return await asyncio.gather(*(request(middleware, path) for path in paths))

    responses = asyncio.run(main())
    return [messages[0]["status"] for messages in responses], peak, responses


def test_never_more_requests_inside_than_the_limit():
    statuses, peak, _ = run_requests(limit=3, max_wait=5, paths=["/events"] * 20, work_seconds=0.01)

    assert statuses == [200] * 20
    assert peak == 3


def test_a_request_that_waits_too_long_gets_503_and_a_retry_hint():
    statuses, peak, responses = run_requests(limit=1, max_wait=0.05, paths=["/events"] * 3, work_seconds=0.3)

    assert statuses == [200, 503, 503]
    assert peak == 1
    rejected = responses[1]
    assert (b"retry-after", b"2") in rejected[0]["headers"]
    assert json.loads(rejected[1]["body"])["error"]["code"] == "service_busy"


def test_a_slot_is_freed_even_when_the_application_fails():
    async def failing(scope, receive, send):
        raise RuntimeError("boom")

    async def main():
        middleware = AdmissionMiddleware(failing, limit=1, max_wait_seconds=0.05)
        outcomes = []
        for _ in range(3):
            try:
                await middleware({"type": "http", "path": "/x"}, None, None)
            except RuntimeError:
                outcomes.append("failed")
        return outcomes

    assert asyncio.run(main()) == ["failed"] * 3  # none was turned away for lack of a slot


def test_exempt_paths_skip_the_queue():
    paths = ["/events", "/health", "/health"]
    statuses, _, _ = run_requests(limit=1, max_wait=0.05, paths=paths, work_seconds=0.3, exempt=frozenset({"/health"}))

    assert statuses == [200, 200, 200]
