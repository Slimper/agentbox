from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import APIError, not_found
from agentbox.api.idempotency import IdempotencyGuard, idempotency
from agentbox.api.pagination import PageParams, list_response, page_params, paginate
from agentbox.api.schemas import WebhookCreate, WebhookUpdate
from agentbox.db.models import Webhook, WebhookDelivery, utcnow
from agentbox.domain.ids import new_id
from agentbox.jobs.queue import enqueue
from agentbox.runtime import Runtime
from agentbox.security.crypto import encrypt_str
from agentbox.services.inboxes import get_inbox
from agentbox.webhooks.delivery import delivery_to_dict, generate_secret, webhook_to_dict

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


async def _get(session: AsyncSession, org_id: str, webhook_id: str) -> Webhook:
    hook = await session.scalar(
        select(Webhook).where(Webhook.id == webhook_id, Webhook.organization_id == org_id, Webhook.deleted_at.is_(None))
    )
    if hook is None:
        raise not_found("webhook", webhook_id)
    return hook


@router.post("", status_code=201)
async def create(
    body: WebhookCreate,
    principal: Principal = Depends(require_scope("webhooks:write")),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    idem: IdempotencyGuard = Depends(idempotency),
):
    if idem.replay:
        return idem.replay
    if body.inbox_id:
        await get_inbox(session, principal.organization_id, body.inbox_id)
    secret = generate_secret()
    hook = Webhook(id=new_id("whk"), organization_id=principal.organization_id, inbox_id=body.inbox_id, url=body.url,
                   secret_encrypted=encrypt_str(runtime.settings.app_secret_key, secret),
                   description=body.description, status="active", event_types=body.event_types)
    session.add(hook)
    await session.commit()
    return await idem.commit(201, {**webhook_to_dict(hook), "secret": secret})


@router.get("")
async def list_webhooks(principal: Principal = Depends(require_scope("webhooks:read")),
                        session: AsyncSession = Depends(get_session), params: PageParams = Depends(page_params)):
    stmt = select(Webhook).where(Webhook.organization_id == principal.organization_id, Webhook.deleted_at.is_(None))
    rows, next_cursor = await paginate(session, stmt, Webhook.id, params)
    return list_response([webhook_to_dict(w) for w in rows], next_cursor)


@router.get("/{webhook_id}")
async def get_one(webhook_id: str, principal: Principal = Depends(require_scope("webhooks:read")),
                  session: AsyncSession = Depends(get_session)):
    return webhook_to_dict(await _get(session, principal.organization_id, webhook_id))


@router.patch("/{webhook_id}")
async def update(webhook_id: str, body: WebhookUpdate, principal: Principal = Depends(require_scope("webhooks:write")),
                 session: AsyncSession = Depends(get_session)):
    hook = await _get(session, principal.organization_id, webhook_id)
    for field_name, value in body.model_dump(exclude_unset=True).items():
        setattr(hook, field_name, value)
    await session.commit()
    return webhook_to_dict(hook)


@router.delete("/{webhook_id}", status_code=204, response_class=Response)
async def delete(webhook_id: str, principal: Principal = Depends(require_scope("webhooks:write")),
                 session: AsyncSession = Depends(get_session)):
    hook = await _get(session, principal.organization_id, webhook_id)
    hook.deleted_at = utcnow()
    hook.status = "disabled"
    await session.commit()
    return Response(status_code=204)


@router.get("/{webhook_id}/deliveries")
async def deliveries(webhook_id: str, principal: Principal = Depends(require_scope("webhooks:read")),
                     session: AsyncSession = Depends(get_session), params: PageParams = Depends(page_params)):
    await _get(session, principal.organization_id, webhook_id)
    stmt = select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook_id,
                                         WebhookDelivery.organization_id == principal.organization_id)
    rows, next_cursor = await paginate(session, stmt, WebhookDelivery.id, params)
    return list_response([delivery_to_dict(d) for d in rows], next_cursor)


@router.post("/{webhook_id}/deliveries/{delivery_id}/retry", status_code=202)
async def retry(webhook_id: str, delivery_id: str, principal: Principal = Depends(require_scope("webhooks:write")),
                session: AsyncSession = Depends(get_session)):
    await _get(session, principal.organization_id, webhook_id)
    original = await session.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id, WebhookDelivery.webhook_id == webhook_id,
                                      WebhookDelivery.organization_id == principal.organization_id)
    )
    if original is None:
        raise not_found("delivery", delivery_id)
    if original.status == "pending":
        raise APIError(409, "conflict", "Delivery is already pending.")
    last = await session.scalar(
        select(WebhookDelivery.attempt_number).where(WebhookDelivery.webhook_id == webhook_id,
                                                     WebhookDelivery.event_id == original.event_id)
        .order_by(WebhookDelivery.attempt_number.desc()).limit(1)
    )
    nxt = WebhookDelivery(id=new_id("wdl"), organization_id=principal.organization_id, webhook_id=webhook_id,
                          event_id=original.event_id, attempt_number=(last or 0) + 1, status="pending",
                          scheduled_at=utcnow())
    session.add(nxt)
    await session.flush()
    await enqueue(session, "webhook_deliver", {"delivery_id": nxt.id})
    await session.commit()
    return delivery_to_dict(nxt)
