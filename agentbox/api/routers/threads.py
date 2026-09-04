from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.api.pagination import PageParams, list_response, page_params
from agentbox.db.models import Message, Thread
from agentbox.services.attachments import attachments_for_messages
from agentbox.services.inboxes import get_inbox
from agentbox.services.messages import message_to_dict
from agentbox.services.threads import get_thread, thread_to_dict

router = APIRouter(prefix="/v1", tags=["threads"])


@router.get("/inboxes/{inbox_id}/threads")
async def list_threads(
    inbox_id: str, principal: Principal = Depends(require_scope("messages:read")),
    session: AsyncSession = Depends(get_session), params: PageParams = Depends(page_params),
):
    await get_inbox(session, principal.organization_id, inbox_id)
    stmt = (select(Thread).where(Thread.organization_id == principal.organization_id, Thread.inbox_id == inbox_id)
            .order_by(Thread.last_message_at.desc(), Thread.id.desc()))
    offset = int(params.cursor) if params.cursor and params.cursor.isdigit() else 0
    rows = list((await session.scalars(stmt.offset(offset).limit(params.limit + 1))).all())
    next_cursor = str(offset + params.limit) if len(rows) > params.limit else None
    return list_response([thread_to_dict(t) for t in rows[: params.limit]], next_cursor)


@router.get("/threads/{thread_id}")
async def get_one(thread_id: str, principal: Principal = Depends(require_scope("messages:read")),
                  session: AsyncSession = Depends(get_session)):
    t = await get_thread(session, principal.organization_id, thread_id)
    msgs = list((await session.scalars(
        select(Message).where(Message.thread_id == t.id, Message.organization_id == principal.organization_id)
        .order_by(Message.created_at, Message.id)
    )).all())
    atts = await attachments_for_messages(session, principal.organization_id, [m.id for m in msgs])
    return {**thread_to_dict(t), "messages": [message_to_dict(m, atts[m.id]) for m in msgs]}
