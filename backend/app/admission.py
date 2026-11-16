"""Admission control: only as many requests inside the application at once as
there are database connections.

Why it is needed. A request keeps its database connection while it hops
between worker threads (authentication, the endpoint, building the response).
Without a limit, a burst lets hundreds of requests each take a turn: some hold
a connection and wait for a thread, the rest hold a thread and wait for a
connection, and nothing moves until the pool times out.

With it, at most `limit` requests are in progress and each is guaranteed a
connection and a thread. The others wait here, in order of arrival, costing
nothing but a little memory. One that waits too long gets a 503 with a hint to
retry, which is an honest answer when the site is over capacity.
"""

import asyncio
import json

from starlette.types import ASGIApp, Receive, Scope, Send

_BUSY_BODY = json.dumps(
    {
        "error": {
            "code": "service_busy",
            "message": "The service is very busy right now. Please try again in a few seconds.",
        }
    }
).encode()


class AdmissionMiddleware:
    def __init__(
        self, app: ASGIApp, limit: int, max_wait_seconds: float, exempt_paths: frozenset[str] = frozenset()
    ) -> None:
        self.app = app
        self._slots = asyncio.Semaphore(limit)
        self._max_wait_seconds = max_wait_seconds
        self._exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._exempt_paths:
            await self.app(scope, receive, send)
            return

        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self._max_wait_seconds)
        except TimeoutError:
            await self._reject(send)
            return
        try:
            # Returns only after the request's cleanup, so the slot is freed
            # exactly when its database connection is.
            await self.app(scope, receive, send)
        finally:
            self._slots.release()

    @staticmethod
    async def _reject(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_BUSY_BODY)).encode()),
                    (b"retry-after", b"2"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _BUSY_BODY})
