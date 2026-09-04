import json

import structlog
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import APIError
from agentbox.db.models import Message, ProviderAccount
from agentbox.providers.router import parse_provider_events
from agentbox.providers.sendgrid import verify_sendgrid_signature
from agentbox.runtime import Runtime
from agentbox.security.crypto import decrypt_json
from agentbox.services.delivery import apply_delivery_status

router = APIRouter(prefix="/v1/providers", tags=["provider-events"])
log = structlog.get_logger("agentbox.provider_events")


@router.post("/{provider}/events/{token}")
async def receive_events(provider: str, token: str, request: Request, session: AsyncSession = Depends(get_session),
                         runtime: Runtime = Depends(get_runtime)):
    account = await session.scalar(select(ProviderAccount).where(ProviderAccount.webhook_token == token,
                                                                 ProviderAccount.provider == provider,
                                                                 ProviderAccount.status == "active"))
    if account is None:
        raise APIError(404, "not_found", "Unknown provider webhook.")
    body = await request.body()
    cfg = decrypt_json(runtime.settings.app_secret_key, account.config_encrypted)
    if provider == "sendgrid" and cfg.get("event_public_key"):
        sig = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
        ts = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")
        if not verify_sendgrid_signature(cfg["event_public_key"], sig, ts, body):
            raise APIError(401, "unauthorized", "Invalid SendGrid event signature.")
    try:
        payload = json.loads(body or b"null")
    except ValueError as e:
        raise APIError(422, "validation_error", "Body must be JSON.") from e
    events = parse_provider_events(provider, payload)
    applied = 0
    for ev in events:
        if not ev.agentbox_message_id:
            continue
        message = await session.scalar(
            select(Message).where(Message.id == ev.agentbox_message_id,
                                  Message.organization_id == account.organization_id).with_for_update(key_share=True)
        )
        if message is None:
            continue
        await apply_delivery_status(session, message, ev.status, source=provider, reason_code=ev.reason_code,
                                    reason_message=ev.reason, recipient=ev.recipient,
                                    extra={"provider_event_id": ev.provider_event_id, "occurred_at": ev.occurred_at})
        applied += 1
    await session.commit()
    log.info("provider_events", provider=provider, received=len(events), applied=applied)
    return {"received": len(events), "applied": applied}
