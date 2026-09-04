from __future__ import annotations

import html as html_lib
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.errors import APIError, not_found
from agentbox.api.schemas import EmailAddress
from agentbox.config import Settings
from agentbox.db.models import Attachment, Inbox, Message, Thread, utcnow
from agentbox.domain.ids import new_id
from agentbox.domain.subject import strip_reply_prefixes
from agentbox.extensions import registry
from agentbox.governance.policies import evaluate_send
from agentbox.jobs.queue import enqueue
from agentbox.services.attachments import attachment_to_dict, bind_attachments_for_send, copy_attachment_reference
from agentbox.services.events import emit
from agentbox.services.threads import create_thread, participants_of, touch_thread
from agentbox.storage.s3 import ObjectStorage

WEBHOOK_BODY_LIMIT = 65536


@dataclass
class OutboundDraft:
    to: list[dict]
    cc: list[dict] = field(default_factory=list)
    bcc: list[dict] = field(default_factory=list)
    reply_to: list[dict] = field(default_factory=list)
    subject: str = ""
    text: str | None = None
    html: str | None = None
    headers: list[list[str]] = field(default_factory=list)
    attachment_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    forward_attachments_from: str | None = None


def addr_dicts(items: list[EmailAddress]) -> list[dict]:
    return [{"email": a.email, "name": a.name} for a in items]


def _fmt(a: dict) -> str:
    return f"{a.get('name')} <{a.get('email')}>" if a.get("name") else str(a.get("email", ""))


def _dedupe(items: list[dict], exclude: set[str]) -> list[dict]:
    seen: set[str] = set(exclude)
    out = []
    for a in items:
        e = (a.get("email") or "").lower()
        if e and e not in seen:
            seen.add(e)
            out.append({"email": e, "name": a.get("name")})
    return out


def truncate_body(s: str | None, limit: int = WEBHOOK_BODY_LIMIT) -> tuple[str | None, bool]:
    if s is None or len(s) <= limit:
        return s, False
    return s[:limit], True


def message_to_dict(m: Message, attachments: list[Attachment], include_headers: bool = False) -> dict:
    d = {
        "id": m.id, "inbox_id": m.inbox_id, "thread_id": m.thread_id, "direction": m.direction, "status": m.status,
        "from": m.from_address, "to": m.to_addresses, "cc": m.cc_addresses, "bcc": m.bcc_addresses,
        "reply_to": m.reply_to_addresses, "subject": m.subject, "text": m.text_body, "html": m.html_body,
        "internet_message_id": m.internet_message_id, "in_reply_to": m.in_reply_to, "references": m.references,
        "provider": m.provider, "provider_message_id": m.provider_message_id, "size_bytes": m.size_bytes,
        "error_code": m.error_code, "error_message": m.error_message, "metadata": m.metadata_,
        "attachments": [attachment_to_dict(a) for a in attachments],
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "created_at": m.created_at.isoformat(), "updated_at": m.updated_at.isoformat(),
    }
    if include_headers:
        d["headers"] = m.headers
    return d


def message_to_event_payload(m: Message, attachments: list[Attachment]) -> dict:
    text, t1 = truncate_body(m.text_body)
    html, t2 = truncate_body(m.html_body)
    body = message_to_dict(m, attachments)
    body["text"], body["html"], body["truncated"] = text, html, t1 or t2
    return {"inbox_id": m.inbox_id, "thread_id": m.thread_id, "message_id": m.id, "message": body}


async def get_message(session: AsyncSession, organization_id: str, message_id: str) -> Message:
    m = await session.scalar(
        select(Message).where(Message.id == message_id, Message.organization_id == organization_id)
    )
    if m is None:
        raise not_found("message", message_id)
    return m


async def create_outbound_message(
    session: AsyncSession, storage: ObjectStorage, settings: Settings, *, organization_id: str, inbox: Inbox,
    draft: OutboundDraft,
) -> Message:
    for hook in registry().hooks(settings, "message.before_send"):
        await hook(session, organization_id, settings)
    to = _dedupe(draft.to, set())
    cc = _dedupe(draft.cc, {a["email"] for a in to})
    bcc = _dedupe(draft.bcc, {a["email"] for a in to + cc})
    if not to:
        raise APIError(422, "validation_error", "At least one recipient in 'to' is required.")
    if not draft.text and not draft.html:
        raise APIError(422, "validation_error", "One of 'text' or 'html' is required.")
    for name, _ in draft.headers:
        if not name.lower().startswith("x-"):
            raise APIError(422, "validation_error", f"Custom header '{name}' must start with 'X-'.")
    forwarded: list[Attachment] = []
    if draft.forward_attachments_from:
        forwarded = list(await session.scalars(
            select(Attachment).where(Attachment.message_id == draft.forward_attachments_from,
                                     Attachment.organization_id == organization_id, Attachment.status == "ready")
        ))
    pending_atts: list[Attachment] = []
    if draft.attachment_ids:
        pending_atts = list(await session.scalars(
            select(Attachment).where(Attachment.id.in_(draft.attachment_ids),
                                     Attachment.organization_id == organization_id)
        ))
    decision = await evaluate_send(session, organization_id=organization_id, inbox=inbox, recipients=to + cc + bcc,
                                   attachments=pending_atts + forwarded, thread_id=draft.thread_id)
    if decision.block is not None:
        b = decision.block
        await emit(session, organization_id=organization_id, resource_type="inbox", resource_id=inbox.id,
                   type="policy.blocked", payload={"inbox_id": inbox.id, "reason": b.reason, "code": b.code,
                                                   "details": b.details, "recipients": [a["email"] for a in to]})
        await session.commit()
        raise APIError(b.http_status, b.code, b.message, {"reason": b.reason, **b.details})
    attachments = await bind_attachments_for_send(session, storage, organization_id=organization_id,
                                                  attachment_ids=draft.attachment_ids)
    total = len(draft.text or "") + len(draft.html or "") + sum(a.size_bytes for a in attachments + forwarded)
    if total > settings.max_outbound_bytes:
        raise APIError(413, "message_too_large", f"Message exceeds {settings.max_outbound_bytes} bytes.")

    now = utcnow()
    msg_id = new_id("msg")
    domain = inbox.address.split("@", 1)[1]
    participants = participants_of(to + cc + bcc, inbox.address)
    if draft.thread_id:
        thread = await session.scalar(
            select(Thread).where(Thread.id == draft.thread_id, Thread.organization_id == organization_id)
        )
        if thread is None:
            raise not_found("thread", draft.thread_id)
    else:
        thread = await create_thread(session, organization_id=organization_id, inbox_id=inbox.id,
                                     subject=draft.subject, participants=participants, at=now)
    touch_thread(thread, participants, now)
    message = Message(
        id=msg_id, organization_id=organization_id, inbox_id=inbox.id, thread_id=thread.id, direction="outbound",
        status="queued", from_address={"email": inbox.address, "name": inbox.display_name}, to_addresses=to,
        cc_addresses=cc, bcc_addresses=bcc, reply_to_addresses=_dedupe(draft.reply_to, set()),
        subject=draft.subject or "", text_body=draft.text, html_body=draft.html,
        internet_message_id=f"<{msg_id}@{domain}>", in_reply_to=draft.in_reply_to, references=draft.references,
        headers=draft.headers, size_bytes=total, metadata_=draft.metadata,
    )
    session.add(message)
    await session.flush()
    for a in attachments:
        a.message_id = msg_id
    for a in forwarded:
        session.add(copy_attachment_reference(a, msg_id))
    await session.flush()
    base = {"inbox_id": inbox.id, "thread_id": thread.id, "message_id": msg_id}
    if decision.approval_required:
        message.status = "pending_approval"
        await emit(session, organization_id=organization_id, resource_type="message", resource_id=msg_id,
                   type="approval.required", payload={**base, "reasons": decision.approval_reasons,
                                                      "to": to, "subject": message.subject})
        return message
    await emit(session, organization_id=organization_id, resource_type="message", resource_id=msg_id,
               type="message.queued", payload=base)
    await enqueue(session, "outbound_send", {"message_id": msg_id})
    return message


async def approve_message(session: AsyncSession, message: Message, *, actor: str) -> Message:
    if message.status != "pending_approval":
        raise APIError(409, "conflict", f"Message is {message.status}, not pending approval.")
    message.status = "queued"
    base = {"inbox_id": message.inbox_id, "thread_id": message.thread_id, "message_id": message.id}
    await emit(session, organization_id=message.organization_id, resource_type="message", resource_id=message.id,
               type="approval.approved", payload={**base, "actor": actor})
    await emit(session, organization_id=message.organization_id, resource_type="message", resource_id=message.id,
               type="message.queued", payload=base)
    await enqueue(session, "outbound_send", {"message_id": message.id})
    return message


async def reject_message(session: AsyncSession, message: Message, *, actor: str, reason: str | None) -> Message:
    if message.status != "pending_approval":
        raise APIError(409, "conflict", f"Message is {message.status}, not pending approval.")
    message.status = "rejected"
    message.error_code = "rejected_by_approver"
    message.error_message = reason
    await emit(session, organization_id=message.organization_id, resource_type="message", resource_id=message.id,
               type="approval.rejected", payload={"inbox_id": message.inbox_id, "thread_id": message.thread_id,
                                                  "message_id": message.id, "actor": actor, "reason": reason})
    return message


def build_reply_draft(
    original: Message, inbox: Inbox, *, text: str | None, html: str | None, reply_all: bool,
    to: list[dict], cc: list[dict], bcc: list[dict], attachment_ids: list[str],
) -> OutboundDraft:
    own = inbox.address.lower()
    if to:
        recipients = list(to)
    elif original.direction == "outbound":
        recipients = list(original.to_addresses)
    else:
        recipients = list(original.reply_to_addresses or [original.from_address])
    cc_list = list(cc)
    if reply_all:
        cc_list += list(original.to_addresses) + list(original.cc_addresses)
    recipients = _dedupe(recipients, {own})
    cc_list = _dedupe(cc_list, {own, *(a["email"] for a in recipients)})
    return OutboundDraft(
        to=recipients, cc=cc_list, bcc=_dedupe(bcc, {own}), subject=f"Re: {strip_reply_prefixes(original.subject)}",
        text=text, html=html, attachment_ids=attachment_ids, thread_id=original.thread_id,
        in_reply_to=original.internet_message_id,
        references=[*original.references, original.internet_message_id][-50:],
    )


def build_forward_draft(
    original: Message, *, to: list[dict], cc: list[dict], bcc: list[dict], text: str | None, html: str | None,
    include_attachments: bool,
) -> OutboundDraft:
    when = (original.received_at or original.sent_at or original.created_at).isoformat()
    header_lines = [
        "---------- Forwarded message ----------",
        f"From: {_fmt(original.from_address)}",
        f"Date: {when}",
        f"Subject: {original.subject}",
        f"To: {', '.join(_fmt(a) for a in original.to_addresses)}",
    ]
    quoted_text = "\n".join(header_lines) + "\n\n" + (original.text_body or "")
    full_text = ((text or "").rstrip() + "\n\n" + quoted_text).strip("\n")
    full_html = None
    if html is not None or original.html_body is not None:
        quoted_html = original.html_body or f"<pre>{html_lib.escape(original.text_body or '')}</pre>"
        info = "<br>".join(html_lib.escape(line) for line in header_lines)
        head = html or html_lib.escape(text or "")
        full_html = f"{head}<br><br><div>{info}</div><blockquote>{quoted_html}</blockquote>"
    return OutboundDraft(
        to=list(to), cc=list(cc), bcc=list(bcc), subject=f"Fwd: {strip_reply_prefixes(original.subject)}",
        text=full_text, html=full_html, forward_attachments_from=original.id if include_attachments else None,
    )
