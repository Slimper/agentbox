from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Inbox, utcnow
from agentbox.jobs.worker import JobContext
from agentbox.services.events import emit
from agentbox.services.inboxes import inbox_to_dict


async def expire_inbox(ctx: JobContext, session: AsyncSession) -> None:
    inbox = await session.scalar(select(Inbox).where(Inbox.id == ctx.payload["inbox_id"]).with_for_update())
    if inbox is None or inbox.status != "active" or inbox.expires_at is None or inbox.expires_at > utcnow():
        return
    inbox.status = "expired"
    await emit(session, organization_id=inbox.organization_id, resource_type="inbox", resource_id=inbox.id,
               type="inbox.expired", payload={"inbox_id": inbox.id, "inbox": inbox_to_dict(inbox)})
