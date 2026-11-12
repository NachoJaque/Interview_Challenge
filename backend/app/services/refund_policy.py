"""Time-based refund policy. Pure functions: no database, easy to unit test."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import Settings


@dataclass(frozen=True)
class RefundDecision:
    allowed: bool
    percentage: int
    reason: str


def _format_hours(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hours"


def evaluate_refund_window(starts_at: datetime, now: datetime, settings: Settings) -> RefundDecision:
    time_left = starts_at - now
    full = _format_hours(settings.full_refund_min_hours)
    partial = _format_hours(settings.partial_refund_min_hours)

    if time_left <= timedelta(0):
        return RefundDecision(False, 0, "The event has already started; refunds are closed.")
    if time_left >= timedelta(hours=settings.full_refund_min_hours):
        return RefundDecision(True, 100, f"Full refund: {full} or more before the event.")
    if time_left >= timedelta(hours=settings.partial_refund_min_hours):
        pct = settings.partial_refund_percentage
        return RefundDecision(True, pct, f"{pct}% refund: between {partial} and {full} before the event.")
    return RefundDecision(False, 0, f"Refunds close {partial} before the event starts.")
