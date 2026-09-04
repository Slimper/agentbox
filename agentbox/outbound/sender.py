import hashlib

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Attachment, DeliveryAttempt, Inbox, Message, utcnow
from agentbox.domain.ids import new_id
from agentbox.extensions import registry
from agentbox.jobs.queue import RetryLater, backoff_for
from agentbox.jobs.worker import JobContext
from agentbox.mime.build import OutboundAttachment, OutboundMessage, build_mime
from agentbox.mime.parse import Address
from agentbox.providers.base import Envelope, PermanentError, TemporaryError
from agentbox.providers.router import build_provider, return_path_for, select_provider_account
from agentbox.services.events import emit

log = structlog.get_logger("agentbox.outbound")


def _addr(d: dict) -> Address:
    return Address(d["email"], d.get("name"))


async def _record_attempt(ctx: JobContext, **fields) -> str:
    async with ctx.runtime.db.session() as s:
        attempt = DeliveryAttempt(id=new_id("dat"), **fields)
        s.add(attempt)
        await s.commit()
        return attempt.id


async def _finish_attempt(ctx: JobContext, attempt_id: str, status: str, *, provider_message_id=None,
                          error_code=None, error_message=None) -> None:
    async with ctx.runtime.db.session() as s:
        attempt = await s.get(DeliveryAttempt, attempt_id)
        attempt.status = status
        attempt.provider_message_id = provider_message_id
        attempt.error_code = error_code
        attempt.error_message = error_message[:4000] if error_message else None
        attempt.finished_at = utcnow()
        await s.commit()


async def send_outbound(ctx: JobContext, session: AsyncSession) -> None:
    message = await session.scalar(
        select(Message).where(Message.id == ctx.payload["message_id"]).with_for_update(key_share=True)
    )
    if message is None or message.status != "queued":
        return
    inbox = await session.get(Inbox, message.inbox_id)
    rows = list((await session.scalars(select(Attachment).where(Attachment.message_id == message.id))).all())
    parts: list[OutboundAttachment] = []
    for a in rows:
        data = await ctx.runtime.storage.get_bytes(a.storage_key)
        if a.sha256 is None:
            a.sha256 = hashlib.sha256(data).hexdigest()
        parts.append(OutboundAttachment(a.filename, a.content_type, data, a.disposition, a.content_id))
    om = OutboundMessage(
        message_id=message.internet_message_id, from_=_addr(message.from_address),
        to=[_addr(a) for a in message.to_addresses], cc=[_addr(a) for a in message.cc_addresses],
        bcc=[_addr(a) for a in message.bcc_addresses], reply_to=[_addr(a) for a in message.reply_to_addresses],
        subject=message.subject, text=message.text_body, html=message.html_body, in_reply_to=message.in_reply_to,
        references=list(message.references), headers=[list(h) for h in message.headers], attachments=parts,
    )
    raw = build_mime(om)
    account = provider = mail_from = None
    for hook in registry().hooks(ctx.runtime.settings, "outbound.provider"):
        found = await hook(session, ctx.runtime, inbox)
        if found:
            provider, mail_from = found
            break
    if provider is None and inbox.provider_mode == "connected":
        from agentbox.connectors.service import smtp_config_for_inbox
        from agentbox.providers.smtp_relay import SMTPRelayProvider

        cfg = await smtp_config_for_inbox(session, ctx.runtime.settings, inbox.id, http=ctx.runtime.http)
        if cfg is None:
            raise PermanentError("mailbox connection is missing or disconnected")
        provider = SMTPRelayProvider(host=cfg["host"], port=cfg["port"], username=cfg["username"],
                                     password=cfg["password"], starttls=cfg["starttls"], use_tls=cfg["use_tls"],
                                     oauth_token=cfg.get("oauth_token"))
        mail_from = cfg["mail_from"]
    if provider is None:
        account = await select_provider_account(session, message.organization_id, inbox_id=inbox.id,
                                                recipient_domain=message.to_addresses[0]["email"].split("@")[-1])
        provider = build_provider(account, ctx.runtime.settings, ctx.runtime.http)
    domain = inbox.address.split("@", 1)[1]
    envelope = Envelope(
        mail_from=mail_from or return_path_for(message.id, domain),
        rcpt_to=[a["email"] for a in message.to_addresses + message.cc_addresses + message.bcc_addresses],
        message_id=message.internet_message_id,
    )
    attempt_id = await _record_attempt(ctx, organization_id=message.organization_id, message_id=message.id,
                                       provider=provider.name, provider_account_id=account.id if account else None,
                                       attempt_number=ctx.attempts, status="started")
    base_payload = {"inbox_id": inbox.id, "thread_id": message.thread_id, "message_id": message.id,
                    "provider": provider.name}
    try:
        result = await provider.send(envelope, om, raw)
    except TemporaryError as e:
        await _finish_attempt(ctx, attempt_id, "temporary_failure", error_code=e.code, error_message=str(e))
        log.warning("outbound_temporary_failure", message_id=message.id, attempt=ctx.attempts, error=str(e))
        if ctx.is_last_attempt:
            message.status = "failed"
            message.error_code, message.error_message = e.code, str(e)[:4000]
            await emit(session, organization_id=message.organization_id, resource_type="message",
                       resource_id=message.id, type="message.failed", payload={**base_payload, "error": str(e)})
            return
        raise RetryLater(backoff_for("outbound_send", ctx.attempts), str(e)) from e
    except PermanentError as e:
        await _finish_attempt(ctx, attempt_id, "permanent_failure", error_code=e.code, error_message=str(e))
        message.status = "rejected"
        message.error_code, message.error_message = e.code, str(e)[:4000]
        await emit(session, organization_id=message.organization_id, resource_type="message",
                   resource_id=message.id, type="message.rejected", payload={**base_payload, "error": str(e)})
        return
    await _finish_attempt(ctx, attempt_id, "accepted", provider_message_id=result.provider_message_id,
                          error_message=(", ".join(f"{k}: {v}" for k, v in result.refused.items()) or None))
    raw_key = f"org/{message.organization_id}/raw/{message.id}.eml"
    await ctx.runtime.storage.put_bytes(raw_key, raw, "message/rfc822")
    message.status = "provider_accepted"
    message.provider = provider.name
    message.provider_message_id = result.provider_message_id
    message.raw_storage_key = raw_key
    message.size_bytes = len(raw)
    message.sent_at = utcnow()
    await emit(session, organization_id=message.organization_id, resource_type="message", resource_id=message.id,
               type="message.provider_accepted", payload={**base_payload, "refused": result.refused})
