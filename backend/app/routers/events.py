from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.audit import audit
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


def _to_read(event: Event, viewer: User | None) -> EventRead:
    """Everyone sees what is available; only admins see how the rest splits
    into reserved and sold, which is the venue's sales data."""
    read = EventRead.model_validate(event)
    if viewer is None or not viewer.is_admin:
        for ticket_type in read.ticket_types:
            ticket_type.held = None
            ticket_type.sold = None
    return read


@router.get("", response_model=list[EventRead])
def list_events(
    db: DbSession,
    viewer: OptionalUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    include_past: Annotated[bool, Query(description="Also list events that have already started.")] = False,
) -> list[EventRead]:
    """Events ordered by date. Upcoming events only, unless `include_past` is set."""
    query = select(Event).options(selectinload(Event.ticket_types))
    if not include_past:
        query = query.where(Event.starts_at > datetime.now(UTC))
    events = db.scalars(query.order_by(Event.starts_at, Event.id).limit(limit).offset(offset))
    return [_to_read(event, viewer) for event in events]


@router.get("/{event_id}", response_model=EventRead)
def get_event(event_id: ResourceId, db: DbSession, viewer: OptionalUser) -> EventRead:
    return _to_read(_get_event(db, event_id), viewer)


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
