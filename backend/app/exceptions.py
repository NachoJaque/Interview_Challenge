"""Errors, and the single place where they become HTTP responses.

Every error the API returns has the same body, whatever raised it:

    {"error": {"code": "insufficient_capacity", "message": "..."}}

`code` is stable and meant for programs; `message` is meant for people.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """A rule was broken. Raised anywhere in the app, mapped to HTTP below."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "domain_error"
    headers: dict[str, str] | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- Authentication and authorization ---------------------------------------------


class NotAuthenticatedError(DomainError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "not_authenticated"
    headers = {"WWW-Authenticate": "Bearer"}


class InvalidCredentialsError(NotAuthenticatedError):
    code = "invalid_credentials"


class ForbiddenError(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


# --- Resources ------------------------------------------------------------------


class NotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class EmailTakenError(ConflictError):
    code = "email_taken"


# --- Reservations and purchases ---------------------------------------------------


class InsufficientCapacityError(ConflictError):
    code = "insufficient_capacity"


class ReservationExpiredError(ConflictError):
    code = "reservation_expired"


class ReservationCancelledError(ConflictError):
    code = "reservation_cancelled"


class PendingReservationExistsError(ConflictError):
    code = "pending_reservation_exists"


class InvalidPurchaseStateError(ConflictError):
    code = "invalid_purchase_state"


class TooManyReservationsError(DomainError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "too_many_reservations"


class TicketLimitExceededError(DomainError):
    code = "ticket_limit_exceeded"


class SalesClosedError(DomainError):
    code = "sales_closed"


class InvalidPurchaseError(DomainError):
    code = "invalid_purchase"


class RefundNotAllowedError(DomainError):
    code = "refund_not_allowed"


# --- HTTP mapping ---------------------------------------------------------------

# For errors raised by the framework itself (unknown route, wrong method, a
# missing bearer token), which carry a status but no code of ours.
_CODES_BY_STATUS = {
    status.HTTP_401_UNAUTHORIZED: NotAuthenticatedError.code,
    status.HTTP_403_FORBIDDEN: ForbiddenError.code,
    status.HTTP_404_NOT_FOUND: NotFoundError.code,
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
}


def _error_response(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: list[dict[str, str]] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


async def _handle_domain_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    return _error_response(exc.status_code, exc.code, exc.message, exc.headers)


async def _handle_http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _CODES_BY_STATUS.get(exc.status_code, "http_error")
    headers = dict(exc.headers) if exc.headers else None
    return _error_response(exc.status_code, code, str(exc.detail), headers)


async def _handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # The rejected input is deliberately left out: it can contain a password.
    details = [
        {"field": ".".join(str(part) for part in error["loc"][1:]), "message": str(error["msg"])}
        for error in exc.errors()
    ]
    message = "Invalid request."
    if details:
        first = details[0]
        message = f"{first['field']}: {first['message']}" if first["field"] else first["message"]
    return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", message, details=details)


async def _handle_database_unavailable(_: Request, exc: Exception) -> JSONResponse:
    """No free connection, a lock or statement timeout, or the database is
    down. The request did not go through, and retrying shortly is the right move."""
    logger.warning("Database unavailable: %s", type(exc).__name__)
    return _error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "service_busy",
        "The service is very busy right now. Please try again in a few seconds.",
        headers={"Retry-After": "2"},
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(PoolTimeoutError, _handle_database_unavailable)
    app.add_exception_handler(OperationalError, _handle_database_unavailable)
    app.add_exception_handler(DomainError, _handle_domain_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
