from fastapi import Request, status
from fastapi.responses import JSONResponse


class DomainError(Exception):
    """Business-rule violation. Raised by services, mapped to HTTP in one place."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "domain_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class InsufficientCapacityError(ConflictError):
    code = "insufficient_capacity"


class ReservationExpiredError(ConflictError):
    code = "reservation_expired"


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


async def domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
