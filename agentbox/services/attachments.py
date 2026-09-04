import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.errors import APIError, not_found
from agentbox.config import Settings
from agentbox.db.models import Attachment, utcnow
from agentbox.domain.ids import new_id
from agentbox.storage.s3 import ObjectStorage

UPLOAD_TTL = timedelta(hours=24)
PRESIGN_PUT_SECONDS = 900
PRESIGN_GET_SECONDS = 600


def storage_key_for(organization_id: str, attachment_id: str) -> str:
    return f"org/{organization_id}/attachments/{attachment_id}"


def attachment_to_dict(a: Attachment) -> dict:
    return {
        "id": a.id, "message_id": a.message_id, "filename": a.filename, "content_type": a.content_type,
        "size_bytes": a.size_bytes, "sha256": a.sha256, "disposition": a.disposition, "content_id": a.content_id,
        "status": a.status, "scan_status": a.scan_status, "created_at": a.created_at.isoformat(),
    }


async def create_upload(
    session: AsyncSession, storage: ObjectStorage, *, organization_id: str, filename: str, content_type: str,
    size_bytes: int, settings: Settings,
) -> tuple[Attachment, str, datetime]:
    if size_bytes <= 0 or size_bytes > settings.max_attachment_bytes:
        raise APIError(413, "attachment_blocked",
                       f"Attachment size must be between 1 and {settings.max_attachment_bytes} bytes.")
    att = Attachment(
        id=new_id("att"), organization_id=organization_id, message_id=None, filename=filename,
        content_type=content_type, size_bytes=size_bytes, status="pending", expires_at=utcnow() + UPLOAD_TTL,
    )
    att.storage_key = storage_key_for(organization_id, att.id)
    session.add(att)
    await session.flush()
    url = await storage.presign_put(att.storage_key, content_type, PRESIGN_PUT_SECONDS)
    return att, url, utcnow() + timedelta(seconds=PRESIGN_PUT_SECONDS)


async def get_attachment(session: AsyncSession, organization_id: str, attachment_id: str) -> Attachment:
    att = await session.scalar(
        select(Attachment).where(Attachment.id == attachment_id, Attachment.organization_id == organization_id)
    )
    if att is None:
        raise not_found("attachment", attachment_id)
    return att


async def bind_attachments_for_send(
    session: AsyncSession, storage: ObjectStorage, *, organization_id: str, attachment_ids: list[str]
) -> list[Attachment]:
    if not attachment_ids:
        return []
    rows = (await session.scalars(
        select(Attachment).where(Attachment.id.in_(attachment_ids), Attachment.organization_id == organization_id)
    )).all()
    by_id = {a.id: a for a in rows}
    for aid in attachment_ids:
        if aid not in by_id:
            raise not_found("attachment", aid)
    out = []
    for aid in attachment_ids:
        a = by_id[aid]
        if a.message_id is not None:
            raise APIError(409, "conflict", f"Attachment '{aid}' is already attached to a message.")
        if a.status == "pending":
            if a.expires_at is not None and a.expires_at < utcnow():
                raise APIError(409, "attachment_blocked", f"Upload for attachment '{aid}' has expired.")
            head = await storage.head(a.storage_key)
            if head is None:
                raise APIError(409, "attachment_blocked", f"Attachment '{aid}' has not been uploaded yet.")
            if head["size"] != a.size_bytes:
                raise APIError(409, "attachment_blocked",
                               f"Attachment '{aid}' uploaded size {head['size']} != declared {a.size_bytes}.")
            a.status = "ready"
            a.expires_at = None
        out.append(a)
    return out


async def store_bytes_attachment(
    session: AsyncSession, storage: ObjectStorage, *, organization_id: str, message_id: str, filename: str,
    content_type: str, data: bytes, disposition: str = "attachment", content_id: str | None = None,
) -> Attachment:
    att = Attachment(
        id=new_id("att"), organization_id=organization_id, message_id=message_id, filename=filename,
        content_type=content_type, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        disposition=disposition, content_id=content_id, status="ready",
    )
    att.storage_key = storage_key_for(organization_id, att.id)
    await storage.put_bytes(att.storage_key, data, content_type)
    session.add(att)
    await session.flush()
    return att


def copy_attachment_reference(original: Attachment, message_id: str) -> Attachment:
    return Attachment(
        id=new_id("att"), organization_id=original.organization_id, message_id=message_id,
        filename=original.filename, content_type=original.content_type, size_bytes=original.size_bytes,
        storage_key=original.storage_key, sha256=original.sha256, disposition=original.disposition,
        content_id=original.content_id, status="ready",
    )


async def attachments_for_messages(
    session: AsyncSession, organization_id: str, message_ids: list[str]
) -> dict[str, list[Attachment]]:
    out: dict[str, list[Attachment]] = {m: [] for m in message_ids}
    if not message_ids:
        return out
    rows = await session.scalars(
        select(Attachment).where(Attachment.organization_id == organization_id,
                                 Attachment.message_id.in_(message_ids)).order_by(Attachment.id)
    )
    for a in rows:
        out[a.message_id].append(a)
    return out
