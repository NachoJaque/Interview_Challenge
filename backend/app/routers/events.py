from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.deps import CurrentAdmin, DbSession, ResourceId
from app.exceptions import ConflictError, NotFoundError
from app.models import Event, TicketType
from app.schemas import EventCreate, EventRead, TicketTypeCreate, TicketTypeRead

router = APIRouter(prefix="/events", tags=["events"])


def _get_event(db: DbSession, event_id: ResourceId) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} not found.")
    return event


@router.get("", response_model=list[EventRead])
def list_events(db: DbSession) -> list[Event]:
    return list(db.scalars(select(Event).options(selectinload(Event.ticket_types)).order_by(Event.starts_at)))


@router.get("/{event_id}", response_model=EventRead)
def get_event(event_id: ResourceId, db: DbSession) -> Event:
    return _get_event(db, event_id)


@router.post("", response_model=EventRead, status_code=status.HTTP_201_CREATED)
def create_event(payload: EventCreate, db: DbSession, _: CurrentAdmin) -> Event:
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
    return event


@router.post("/{event_id}/ticket-types", response_model=TicketTypeRead, status_code=status.HTTP_201_CREATED)
def add_ticket_type(event_id: ResourceId, payload: TicketTypeCreate, db: DbSession, _: CurrentAdmin) -> TicketType:
    """Add a new ticket category (e.g. 'Platinum') to an existing event."""
    event = _get_event(db, event_id)
    ticket_type = TicketType(event_id=event.id, **payload.model_dump())
    db.add(ticket_type)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(f"Event {event_id} already has a ticket type named '{payload.name}'.") from None
    return ticket_type
