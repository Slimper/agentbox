import json
import secrets
import time
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Event, Webhook, WebhookDelivery, utcnow
from agentbox.domain.ids import new_id
from agentbox.jobs.queue import enqueue
from agentbox.jobs.worker import JobContext
from agentbox.security.crypto import decrypt_str
from agentbox.services.events import emit
from agentbox.webhooks.signing import signature_header

RETRY_SCHEDULE = [10, 60, 300, 1800, 7200, 28800, 86400]
MAX_ATTEMPTS = 1 + len(RETRY_SCHEDULE)
USER_AGENT = "AgentBox-Webhooks/1.0"


def generate_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


def event_payload(event: Event) -> dict:
    return {"id": event.id, "type": event.type, "created_at": event.created_at.isoformat(), "data": event.payload}


def webhook_to_dict(w: Webhook) -> dict:
    return {"id": w.id, "url": w.url, "inbox_id": w.inbox_id, "description": w.description, "status": w.status,
            "event_types": w.event_types, "created_at": w.created_at.isoformat(),
            "updated_at": w.updated_at.isoformat()}


def delivery_to_dict(d: WebhookDelivery) -> dict:
    return {"id": d.id, "webhook_id": d.webhook_id, "event_id": d.event_id, "attempt_number": d.attempt_number,
            "status": d.status, "response_status": d.response_status, "response_excerpt": d.response_excerpt,
            "error": d.error, "scheduled_at": d.scheduled_at.isoformat(),
            "started_at": d.started_at.isoformat() if d.started_at else None,
            "finished_at": d.finished_at.isoformat() if d.finished_at else None}


def matches(hook: Webhook, event_type: str, inbox_id: str | None) -> bool:
    if hook.inbox_id and hook.inbox_id != inbox_id:
        return False
    return "*" in hook.event_types or event_type in hook.event_types


async def deliver_webhooks(ctx: JobContext, session: AsyncSession) -> None:
    if "event_id" in ctx.payload:
        await fan_out(ctx, session, ctx.payload["event_id"])
    else:
        await attempt(ctx, session, ctx.payload["delivery_id"])


async def fan_out(ctx: JobContext, session: AsyncSession, event_id: str) -> None:
    event = await session.get(Event, event_id)
    if event is None:
        return
    inbox_id = event.payload.get("inbox_id") if isinstance(event.payload, dict) else None
    hooks = await session.scalars(
        select(Webhook).where(Webhook.organization_id == event.organization_id, Webhook.status == "active",
                              Webhook.deleted_at.is_(None))
    )
    for hook in hooks:
        if not matches(hook, event.type, inbox_id):
            continue
        exists = await session.scalar(
            select(WebhookDelivery.id).where(WebhookDelivery.webhook_id == hook.id,
                                             WebhookDelivery.event_id == event.id)
        )
        if exists:
            continue
        delivery = WebhookDelivery(id=new_id("wdl"), organization_id=event.organization_id, webhook_id=hook.id,
                                   event_id=event.id, attempt_number=1, status="pending", scheduled_at=utcnow())
        session.add(delivery)
        await session.flush()
        await _deliver(ctx, session, hook, event, delivery)


async def attempt(ctx: JobContext, session: AsyncSession, delivery_id: str) -> None:
    delivery = await session.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.status != "pending":
        return
    hook = await session.get(Webhook, delivery.webhook_id)
    event = await session.get(Event, delivery.event_id)
    if hook is None or event is None or hook.status != "active" or hook.deleted_at is not None:
        delivery.status = "failed"
        delivery.error = "webhook disabled or deleted"
        delivery.finished_at = utcnow()
        return
    await _deliver(ctx, session, hook, event, delivery)


async def _deliver(ctx: JobContext, session: AsyncSession, hook: Webhook, event: Event,
                   delivery: WebhookDelivery) -> None:
    secret = decrypt_str(ctx.runtime.settings.app_secret_key, hook.secret_encrypted)
    body = json.dumps(event_payload(event), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ts = int(time.time())
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "AgentBox-Event-Id": event.id,
               "AgentBox-Signature": signature_header(secret, body, ts)}
    delivery.started_at = utcnow()
    ok, error = False, None
    try:
        resp = await ctx.runtime.http.post(hook.url, content=body, headers=headers, timeout=10.0)
        delivery.response_status = resp.status_code
        delivery.response_excerpt = resp.text[:1000]
        ok = 200 <= resp.status_code < 300
        if not ok:
            error = f"HTTP {resp.status_code}"
    except httpx.HTTPError as e:
        error = f"{type(e).__name__}: {e}"[:1000]
    delivery.finished_at = utcnow()
    if ok:
        delivery.status = "succeeded"
        return
    delivery.error = error
    if delivery.attempt_number >= MAX_ATTEMPTS:
        delivery.status = "exhausted"
        await emit(session, organization_id=hook.organization_id, resource_type="webhook", resource_id=hook.id,
                   type="webhook.failed", payload={"webhook_id": hook.id, "event_id": event.id,
                                                   "attempts": delivery.attempt_number, "error": error})
        return
    delivery.status = "failed"
    delay = RETRY_SCHEDULE[delivery.attempt_number - 1]
    nxt = WebhookDelivery(id=new_id("wdl"), organization_id=hook.organization_id, webhook_id=hook.id,
                          event_id=event.id, attempt_number=delivery.attempt_number + 1, status="pending",
                          scheduled_at=utcnow() + timedelta(seconds=delay))
    session.add(nxt)
    await session.flush()
    await enqueue(session, "webhook_deliver", {"delivery_id": nxt.id}, run_at=nxt.scheduled_at)
