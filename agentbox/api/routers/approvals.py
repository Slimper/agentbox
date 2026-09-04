from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.api.pagination import PageParams, list_response, page_params, paginate
from agentbox.api.schemas import ApprovalReject
from agentbox.db.models import Message
from agentbox.services.attachments import attachments_for_messages
from agentbox.services.messages import approve_message, get_message, message_to_dict, reject_message

router = APIRouter(prefix="/v1", tags=["approvals"])


@router.get("/approvals")
async def list_pending(principal: Principal = Depends(require_scope("messages:read")),
                       session: AsyncSession = Depends(get_session), params: PageParams = Depends(page_params)):
    stmt = select(Message).where(Message.organization_id == principal.organization_id,
                                 Message.status == "pending_approval")
    rows, next_cursor = await paginate(session, stmt, Message.id, params)
    atts = await attachments_for_messages(session, principal.organization_id, [m.id for m in rows])
    return list_response([message_to_dict(m, atts[m.id]) for m in rows], next_cursor)


@router.post("/messages/{message_id}/approve", status_code=202)
async def approve(message_id: str, principal: Principal = Depends(require_scope("approvals:write")),
                  session: AsyncSession = Depends(get_session)):
    m = await get_message(session, principal.organization_id, message_id)
    await approve_message(session, m, actor=principal.api_key_id)
    await session.commit()
    return {"id": m.id, "thread_id": m.thread_id, "status": m.status}


@router.post("/messages/{message_id}/reject")
async def reject(message_id: str, body: ApprovalReject,
                 principal: Principal = Depends(require_scope("approvals:write")),
                 session: AsyncSession = Depends(get_session)):
    m = await get_message(session, principal.organization_id, message_id)
    await reject_message(session, m, actor=principal.api_key_id, reason=body.reason)
    await session.commit()
    return {"id": m.id, "thread_id": m.thread_id, "status": m.status}
