"""Initial data: `python -m app.seed`. Safe to run more than once.

Always creates the admin account (when ADMIN_EMAIL and ADMIN_PASSWORD are
set). The demo customer and demo events are loaded only when SEED_DEMO_DATA
is true, so a real deployment starts empty.

Each event is dated relative to "now" so that every refund rule can be shown
live: full refund, partial refund, refunds closed, a non-refundable ticket
type and an almost sold-out event.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Event, TicketType, User
from app.security import hash_password


def _ticket(name: str, price: str, capacity: int, refundable: bool = True) -> TicketType:
    return TicketType(name=name, price=Decimal(price), capacity=capacity, is_refundable=refundable)


def _demo_events(now: datetime) -> list[Event]:
    return [
        Event(
            name="Summer Rock Festival",
            description="More than 7 days away: refunds are paid at 100%.",
            venue="Krunchbox Arena",
            starts_at=now + timedelta(days=30),
            ticket_types=[_ticket("General", "50.00", 200), _ticket("VIP", "150.00", 30)],
        ),
        Event(
            name="Jazz Night",
            description="Between 48 hours and 7 days away: refunds are paid at 50%.",
            venue="Blue Room",
            starts_at=now + timedelta(days=4),
            ticket_types=[_ticket("General", "35.00", 80), _ticket("VIP", "90.00", 12)],
        ),
        Event(
            name="Stand-up Comedy Special",
            description="Less than 48 hours away: tickets can be bought but no longer refunded.",
            venue="The Cellar",
            starts_at=now + timedelta(hours=26),
            ticket_types=[_ticket("General", "25.00", 60), _ticket("VIP", "60.00", 10)],
        ),
        Event(
            name="Indie Showcase (last tickets)",
            description="Almost sold out, ideal for showing two buyers competing for the last tickets.",
            venue="Warehouse 9",
            starts_at=now + timedelta(days=14),
            ticket_types=[_ticket("General", "20.00", 5), _ticket("VIP", "45.00", 2)],
        ),
        Event(
            name="Tech Conference 2027",
            description="Three ticket types. 'Early Bird' is discounted and non-refundable.",
            venue="Convention Center",
            starts_at=now + timedelta(days=60),
            ticket_types=[
                _ticket("Early Bird", "80.00", 50, refundable=False),
                _ticket("General", "120.00", 300),
                _ticket("VIP", "350.00", 40),
            ],
        ),
    ]


def _ensure_user(db, email: str | None, password: str | None, full_name: str, is_admin: bool) -> None:
    if not email or not password:
        return
    if db.scalar(select(User).where(User.email == email.lower())):
        return
    db.add(User(email=email.lower(), full_name=full_name, hashed_password=hash_password(password), is_admin=is_admin))
    print(f"Created {'admin' if is_admin else 'demo'} user {email.lower()}")


def main() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        _ensure_user(db, settings.admin_email, settings.admin_password, "Venue Admin", is_admin=True)
        if not settings.seed_demo_data:
            db.commit()
            return
        _ensure_user(db, settings.demo_email, settings.demo_password, "Demo Customer", is_admin=False)
        if db.scalar(select(Event.id).limit(1)) is None:
            events = _demo_events(datetime.now(UTC))
            db.add_all(events)
            print(f"Created {len(events)} demo events")
        db.commit()


if __name__ == "__main__":
    main()
