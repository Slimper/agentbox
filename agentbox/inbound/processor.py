import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import InboundIngest, Inbox, Message, utcnow
from agentbox.domain.ids import new_id
from agentbox.domain.subject import normalize_subject, strip_reply_prefixes
from agentbox.jobs.worker import JobContext
from agentbox.mime.dsn import classify_dsn, parse_dsn
from agentbox.mime.parse import parse_mime
from agentbox.services.attachments import store_bytes_attachment
from agentbox.services.delivery import apply_delivery_status
from agentbox.services.events import emit
from agentbox.services.messages import message_to_event_payload
from agentbox.services.threads import create_thread, find_thread_for_inbound, participants_of, touch_thread

log = structlog.get_logger("agentbox.inbound")


async def process_inbound(ctx: JobContext, session: AsyncSession) -> None:
    ingest = await session.scalar(
        select(InboundIngest).where(InboundIngest.id == ctx.payload["ingest_id"]).with_for_update(key_share=True)
    )
    if ingest is None or ingest.status != "received":
        return
    raw = await ctx.runtime.storage.get_bytes(ingest.storage_key)
    if ingest.kind == "bounce":
        status = await _process_bounce(ctx, session, ingest, raw)
    else:
        status = await _process_message(ctx, session, ingest, raw)
    ingest.status = status
    ingest.processed_at = utcnow()


async def _process_message(ctx: JobContext, session: AsyncSession, ingest: InboundIngest, raw: bytes) -> str:
    inbox = await session.get(Inbox, ingest.inbox_id)
    parsed = parse_mime(raw)
    domain = inbox.address.split("@", 1)[1]
    mid = parsed.message_id or f"<{ingest.id}@{domain}>"
    dup = await session.scalar(
        select(Message.id).where(Message.organization_id == inbox.organization_id, Message.inbox_id == inbox.id,
                                 Message.internet_message_id == mid)
    )
    if dup:
        ingest.message_id = dup
        return "duplicate"
    from_addresses = [a.to_dict() for a in parsed.from_] or [{"email": ingest.mail_from, "name": None}]
    to_list, cc_list = [a.to_dict() for a in parsed.to], [a.to_dict() for a in parsed.cc]
    participants = participants_of(from_addresses + to_list + cc_list, inbox.address)
    now = utcnow()
    thread = await find_thread_for_inbound(
        session, organization_id=inbox.organization_id, inbox_id=inbox.id, in_reply_to=parsed.in_reply_to,
        references=parsed.references, subject_normalized=normalize_subject(parsed.subject),
        participants=participants, now=now,
    )
    if thread is None:
        thread = await create_thread(session, organization_id=inbox.organization_id, inbox_id=inbox.id,
                                     subject=strip_reply_prefixes(parsed.subject) or "(no subject)",
                                     participants=participants, at=now)
    touch_thread(thread, participants, now)
    message = Message(
        id=new_id("msg"), organization_id=inbox.organization_id, inbox_id=inbox.id, thread_id=thread.id,
        direction="inbound", status="stored", from_address=from_addresses[0], to_addresses=to_list,
        cc_addresses=cc_list, bcc_addresses=[], reply_to_addresses=[a.to_dict() for a in parsed.reply_to],
        subject=parsed.subject, text_body=parsed.text, html_body=parsed.html, internet_message_id=mid,
        in_reply_to=parsed.in_reply_to, references=parsed.references,
        headers=[[k, v] for k, v in parsed.headers], raw_storage_key=ingest.storage_key, size_bytes=len(raw),
        received_at=parsed.date or now,
    )
    session.add(message)
    await session.flush()
    stored = []
    for part in parsed.attachments:
        if len(part.content) > ctx.runtime.settings.max_attachment_bytes:
            await emit(session, organization_id=inbox.organization_id, resource_type="message",
                       resource_id=message.id, type="attachment.blocked",
                       payload={"inbox_id": inbox.id, "message_id": message.id, "filename": part.filename,
                                "size_bytes": len(part.content), "reason": "too_large"})
            continue
        stored.append(await store_bytes_attachment(
            session, ctx.runtime.storage, organization_id=inbox.organization_id, message_id=message.id,
            filename=part.filename, content_type=part.content_type, data=part.content,
            disposition=part.disposition, content_id=part.content_id,
        ))
    ingest.message_id = message.id
    await emit(session, organization_id=inbox.organization_id, resource_type="message", resource_id=message.id,
               type="message.received", payload=message_to_event_payload(message, stored))
    log.info("inbound_stored", message_id=message.id, inbox_id=inbox.id, thread_id=thread.id)
    return "stored"


async def _process_bounce(ctx: JobContext, session: AsyncSession, ingest: InboundIngest, raw: bytes) -> str:
    message = await session.scalar(
        select(Message).where(Message.id == ingest.bounce_message_id).with_for_update(key_share=True)
    )
    if message is None:
        return "failed"
    recipients = parse_dsn(raw)
    outcome = classify_dsn(recipients)
    new_status = "deferred" if outcome == "deferred" else "bounced"
    first = recipients[0] if recipients else None
    reason_code = "unparsed_bounce" if outcome == "unknown" else (first.status if first else None)
    await apply_delivery_status(
        session, message, new_status, source="dsn", reason_code=reason_code,
        reason_message=first.diagnostic if first else None, recipient=first.recipient if first else None,
        extra={"bounce": {"raw_storage_key": ingest.storage_key, "outcome": outcome,
                          "recipients": [r.to_dict() for r in recipients]}},
    )
    return "stored"
