from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Event
from agentbox.domain.ids import new_id
from agentbox.jobs.queue import enqueue

NO_FANOUT = {"webhook.failed"}


async def emit(
    session: AsyncSession, *, organization_id: str, resource_type: str, resource_id: str, type: str, payload: dict
) -> Event:
    event = Event(id=new_id("evt"), organization_id=organization_id, resource_type=resource_type,
                  resource_id=resource_id, type=type, payload=payload)
    session.add(event)
    await session.flush()
    if type not in NO_FANOUT:
        await enqueue(session, "webhook_deliver", {"event_id": event.id})
    return event


def event_to_dict(event: Event) -> dict:
    return {"id": event.id, "type": event.type, "resource_type": event.resource_type,
            "resource_id": event.resource_id, "created_at": event.created_at.isoformat(), "data": event.payload}
