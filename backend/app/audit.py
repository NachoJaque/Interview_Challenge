"""Audit trail: one JSON line per business event (who did what, to which
purchase), written to the `audit` logger.

Each line carries the id of the HTTP request that caused it; the same id is
returned to the client in the X-Request-ID header, so a support case can be
traced from the response back to the log.
"""

import json
import logging
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("audit")

# "-" for work that no request triggered (the background sweeper, the seed).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def audit(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, "request_id": request_id_var.get(), **fields}, default=str))
