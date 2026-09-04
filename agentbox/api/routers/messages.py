import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_runtime, get_session
from agentbox.api.idempotency import IdempotencyGuard, idempotency
from agentbox.api.pagination import PageParams, list_response, page_params, paginate
from agentbox.api.schemas import MessageForward, MessageReply, MessageSend
from agentbox.db.models import Message
from agentbox.runtime import Runtime
from agentbox.services.attachments import attachments_for_messages
from agentbox.services.inboxes import get_active_inbox_for_send, get_inbox
from agentbox.services.messages import (
    OutboundDraft,
    addr_dicts,
    build_forward_draft,
    build_reply_draft,
    create_outbound_message,
    get_message,
    message_to_dict,
)

router = APIRouter(prefix="/v1", tags=["messages"])


def _queued(m: Message) -> dict:
    return {"id": m.id, "thread_id": m.thread_id, "status": m.status, "created_at": m.created_at.isoformat()}


@router.post("/inboxes/{inbox_id}/messages", status_code=202)
async def send(
    inbox_id: str, body: MessageSend,
    principal: Principal = Depends(require_scope("messages:send")),
    session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime),
    idem: IdempotencyGuard = Depends(idempotency),
):
    if idem.replay:
        return idem.replay
    inbox = await get_active_inbox_for_send(session, principal.organization_id, inbox_id)
    draft = OutboundDraft(
        to=addr_dicts(body.to), cc=addr_dicts(body.cc), bcc=addr_dicts(body.bcc), reply_to=addr_dicts(body.reply_to),
        subject=body.subject, text=body.text, html=body.html, headers=[[k, v] for k, v in body.headers.items()],
        attachment_ids=body.attachment_ids, metadata=body.metadata,
    )
    message = await create_outbound_message(session, runtime.storage, runtime.settings,
                                            organization_id=principal.organization_id, inbox=inbox, draft=draft)
    await session.commit()
    return await idem.commit(202, _queued(message))


@router.get("/inboxes/{inbox_id}/messages")
async def list_messages(
    inbox_id: str,
    direction: str | None = None, status: str | None = None, thread_id: str | None = None,
    from_: str | None = Query(None, alias="from"), to: str | None = None,
    since: datetime | None = None, until: datetime | None = None,
    wait: int = Query(0, ge=0, le=60),
    principal: Principal = Depends(require_scope("messages:read")),
    session: AsyncSession = Depends(get_session), params: PageParams = Depends(page_params),
):
    await get_inbox(session, principal.organization_id, inbox_id)
    stmt = select(Message).where(Message.organization_id == principal.organization_id, Message.inbox_id == inbox_id)
    if direction:
        stmt = stmt.where(Message.direction == direction)
    if status:
        stmt = stmt.where(Message.status == status)
    if thread_id:
        stmt = stmt.where(Message.thread_id == thread_id)
    if from_:
        stmt = stmt.where(Message.from_address["email"].astext == from_.lower())
    if to:
        stmt = stmt.where(Message.to_addresses.contains([{"email": to.lower()}]))
    if since:
        stmt = stmt.where(Message.created_at >= since)
    if until:
        stmt = stmt.where(Message.created_at < until)
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        rows, next_cursor = await paginate(session, stmt, Message.id, params)
        if rows or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(1.0)
    atts = await attachments_for_messages(session, principal.organization_id, [m.id for m in rows])
    return list_response([message_to_dict(m, atts[m.id]) for m in rows], next_cursor)


@router.get("/messages/{message_id}")
async def get_one(
    message_id: str, include: str | None = None,
    principal: Principal = Depends(require_scope("messages:read")), session: AsyncSession = Depends(get_session),
):
    m = await get_message(session, principal.organization_id, message_id)
    atts = await attachments_for_messages(session, principal.organization_id, [m.id])
    return message_to_dict(m, atts[m.id], include_headers=(include == "headers"))


@router.post("/messages/{message_id}/reply", status_code=202)
async def reply(
    message_id: str, body: MessageReply,
    principal: Principal = Depends(require_scope("messages:send")),
    session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime),
    idem: IdempotencyGuard = Depends(idempotency),
):
    if idem.replay:
        return idem.replay
    original = await get_message(session, principal.organization_id, message_id)
    inbox = await get_active_inbox_for_send(session, principal.organization_id, original.inbox_id)
    draft = build_reply_draft(original, inbox, text=body.text, html=body.html, reply_all=body.reply_all,
                              to=addr_dicts(body.to), cc=addr_dicts(body.cc), bcc=addr_dicts(body.bcc),
                              attachment_ids=body.attachment_ids)
    message = await create_outbound_message(session, runtime.storage, runtime.settings,
                                            organization_id=principal.organization_id, inbox=inbox, draft=draft)
    await session.commit()
    return await idem.commit(202, _queued(message))


@router.post("/messages/{message_id}/forward", status_code=202)
async def forward(
    message_id: str, body: MessageForward,
    principal: Principal = Depends(require_scope("messages:send")),
    session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime),
    idem: IdempotencyGuard = Depends(idempotency),
):
    if idem.replay:
        return idem.replay
    original = await get_message(session, principal.organization_id, message_id)
    inbox = await get_active_inbox_for_send(session, principal.organization_id, original.inbox_id)
    draft = build_forward_draft(original, to=addr_dicts(body.to), cc=addr_dicts(body.cc), bcc=addr_dicts(body.bcc),
                                text=body.text, html=body.html, include_attachments=body.include_attachments)
    message = await create_outbound_message(session, runtime.storage, runtime.settings,
                                            organization_id=principal.organization_id, inbox=inbox, draft=draft)
    await session.commit()
    return await idem.commit(202, _queued(message))
