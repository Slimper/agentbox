from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import not_found
from agentbox.api.schemas import AttachmentUploadCreate
from agentbox.db.models import Attachment, Message, utcnow
from agentbox.runtime import Runtime
from agentbox.services.attachments import (
    PRESIGN_GET_SECONDS,
    attachment_to_dict,
    create_upload,
    get_attachment,
)

router = APIRouter(prefix="/v1", tags=["attachments"])


@router.post("/attachments/uploads", status_code=201)
async def create_upload_url(
    body: AttachmentUploadCreate,
    principal: Principal = Depends(require_scope("attachments:write")),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
):
    att, url, expires = await create_upload(
        session, runtime.storage, organization_id=principal.organization_id, filename=body.filename,
        content_type=body.content_type, size_bytes=body.size_bytes, settings=runtime.settings,
    )
    await session.commit()
    return {"attachment_id": att.id, "upload_url": url, "expires_at": expires.isoformat(),
            "headers": {"Content-Type": body.content_type}}


@router.get("/attachments/{attachment_id}")
async def get_one(attachment_id: str, principal: Principal = Depends(require_scope("attachments:read")),
                  session: AsyncSession = Depends(get_session)):
    return attachment_to_dict(await get_attachment(session, principal.organization_id, attachment_id))


@router.get("/attachments/{attachment_id}/download")
async def download(
    attachment_id: str, redirect: bool = False,
    principal: Principal = Depends(require_scope("attachments:read")),
    session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime),
):
    att = await get_attachment(session, principal.organization_id, attachment_id)
    url = await runtime.storage.presign_get(att.storage_key, att.filename, PRESIGN_GET_SECONDS)
    if redirect:
        return RedirectResponse(url, status_code=302)
    return {"url": url, "expires_at": (utcnow() + timedelta(seconds=PRESIGN_GET_SECONDS)).isoformat()}


@router.get("/messages/{message_id}/attachments")
async def list_for_message(
    message_id: str, principal: Principal = Depends(require_scope("attachments:read")),
    session: AsyncSession = Depends(get_session),
):
    msg = await session.scalar(
        select(Message.id).where(Message.id == message_id, Message.organization_id == principal.organization_id)
    )
    if msg is None:
        raise not_found("message", message_id)
    rows = await session.scalars(
        select(Attachment).where(Attachment.message_id == message_id,
                                 Attachment.organization_id == principal.organization_id).order_by(Attachment.id)
    )
    return {"data": [attachment_to_dict(a) for a in rows], "next_cursor": None}
