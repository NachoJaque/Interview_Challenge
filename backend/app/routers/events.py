from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.audit import audit
from app.cache import TTLCache
from app.config import get_settings
from app.deps import CurrentAdmin, DbSession, OptionalUser, ResourceId
from app.exceptions import ConflictError, NotFoundError
from app.models import Event, TicketType, User
from app.schemas import EventCreate, EventRead, TicketTypeCreate, TicketTypeRead

router = APIRouter(prefix="/events", tags=["events"])


def _get_event(db: DbSession, event_id: ResourceId) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    return event


# Every open page polls these two endpoints, so their database work is shared
# for a moment. What is cached is the full data; each caller gets its own view.
events_cache = TTLCache(get_settings().events_cache_seconds)


def _for_viewer(event: EventRead, viewer: User | None) -> EventRead:
    """Everyone sees what is available; only admins see how the rest splits
    into reserved and sold, which is the venue's sales data."""
    if viewer is not None and viewer.is_admin:
        return event
    public_ticket_types = [tt.model_copy(update={"held": None, "sold": None}) for tt in event.ticket_types]
    return event.model_copy(update={"ticket_types": public_ticket_types})


@router.get("", response_model=list[EventRead])
def list_events(
    db: DbSession,
    viewer: OptionalUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    include_past: Annotated[bool, Query(description="Also list events that have already started.")] = False,
) -> list[EventRead]:
    """Events ordered by date. Upcoming events only, unless `include_past` is set.

    Availability may be up to a second old.
    """

    def load() -> list[EventRead]:
        query = select(Event).options(selectinload(Event.ticket_types))
        if not include_past:
            query = query.where(Event.starts_at > datetime.now(UTC))
        events = db.scalars(query.order_by(Event.starts_at, Event.id).limit(limit).offset(offset))
        return [EventRead.model_validate(event) for event in events]

    events = events_cache.get_or_load(("list", limit, offset, include_past), load)
    return [_for_viewer(event, viewer) for event in events]


@router.get("/{event_id}", response_model=EventRead)
def get_event(event_id: ResourceId, db: DbSession, viewer: OptionalUser) -> EventRead:
    """One event. Availability may be up to a second old."""
    event = events_cache.get_or_load(("event", event_id), lambda: EventRead.model_validate(_get_event(db, event_id)))
    return _for_viewer(event, viewer)


@router.post("", response_model=EventRead, status_code=status.HTTP_201_CREATED)
def create_event(payload: EventCreate, db: DbSession, admin: CurrentAdmin) -> Event:
    names = [tt.name.lower() for tt in payload.ticket_types]
    if len(set(names)) != len(names):
        raise ConflictError("Ticket type names must be unique within an event.")
    event = Event(
        name=payload.name,
        description=payload.description,
        venue=payload.venue,
        starts_at=payload.starts_at,
        ticket_types=[TicketType(**tt.model_dump()) for tt in payload.ticket_types],
    )
    db.add(event)
    db.commit()
    audit("event_created", event_id=event.id, admin_id=admin.id)
    return event


@router.post("/{event_id}/ticket-types", response_model=TicketTypeRead, status_code=status.HTTP_201_CREATED)
def add_ticket_type(event_id: ResourceId, payload: TicketTypeCreate, db: DbSession, admin: CurrentAdmin) -> TicketType:
    """Add a new ticket category (e.g. 'Platinum') to an existing event."""
    event = _get_event(db, event_id)
    ticket_type = TicketType(event_id=event.id, **payload.model_dump())
    db.add(ticket_type)
    try:
        db.commit()
    except IntegrityError:
        # The unique index compares names case-insensitively.
        db.rollback()
        raise ConflictError(f"Event {event_id} already has a ticket type named '{payload.name}'.") from None
    audit("ticket_type_added", ticket_type_id=ticket_type.id, event_id=event.id, admin_id=admin.id)
    return ticket_type
