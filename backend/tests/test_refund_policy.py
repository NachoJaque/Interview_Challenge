from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.services.refund_policy import evaluate_refund_window

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SETTINGS = Settings(_env_file=None)


@pytest.mark.parametrize(
    ("time_left", "allowed", "percentage"),
    [
        (timedelta(days=30), True, 100),
        (timedelta(days=7), True, 100),  # boundary: exactly 7 days is still a full refund
        (timedelta(days=7) - timedelta(seconds=1), True, 50),
        (timedelta(hours=48), True, 50),  # boundary: exactly 48 hours is still refundable
        (timedelta(hours=48) - timedelta(seconds=1), False, 0),
        (timedelta(hours=1), False, 0),
        (timedelta(0), False, 0),  # the event is starting right now
        (timedelta(days=-1), False, 0),  # the event already happened
    ],
)
def test_refund_window(time_left: timedelta, allowed: bool, percentage: int) -> None:
    decision = evaluate_refund_window(NOW + time_left, NOW, SETTINGS)

    assert decision.allowed is allowed
    assert decision.percentage == percentage
    assert decision.reason


def test_refund_window_is_configurable() -> None:
    lenient = Settings(
        _env_file=None, full_refund_min_hours=24, partial_refund_min_hours=2, partial_refund_percentage=80
    )

    assert evaluate_refund_window(NOW + timedelta(hours=30), NOW, lenient).percentage == 100
    assert evaluate_refund_window(NOW + timedelta(hours=3), NOW, lenient).percentage == 80
    assert not evaluate_refund_window(NOW + timedelta(hours=1), NOW, lenient).allowed
