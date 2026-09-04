from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session, get_settings_dep
from agentbox.api.errors import APIError
from agentbox.api.idempotency import IdempotencyGuard, idempotency
from agentbox.api.pagination import PageParams, list_response, page_params, paginate
from agentbox.api.schemas import InboxCreate, InboxOut
from agentbox.config import Settings
from agentbox.db.models import Domain, Inbox, utcnow
from agentbox.services.events import emit
from agentbox.services.inboxes import create_inbox, get_inbox, inbox_to_dict

router = APIRouter(prefix="/v1/inboxes", tags=["inboxes"])


@router.post("", status_code=201, response_model=InboxOut)
async def create(
    body: InboxCreate,
    principal: Principal = Depends(require_scope("inboxes:write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    idem: IdempotencyGuard = Depends(idempotency),
):
    if idem.replay:
        return idem.replay
    inbox = await create_inbox(
        session, organization_id=principal.organization_id, settings=settings, username=body.username,
        domain=body.domain, display_name=body.display_name, metadata=body.metadata, ttl=body.ttl,
    )
    await session.commit()
    return await idem.commit(201, inbox_to_dict(inbox))


@router.get("")
async def list_inboxes(
    request: Request,
    status: str | None = None,
    domain: str | None = None,
    principal: Principal = Depends(require_scope("inboxes:read")),
    session: AsyncSession = Depends(get_session),
    params: PageParams = Depends(page_params),
):
    stmt = select(Inbox).where(Inbox.organization_id == principal.organization_id, Inbox.deleted_at.is_(None))
    if status:
        stmt = stmt.where(Inbox.status == status)
    if domain:
        stmt = stmt.join(Domain, Domain.id == Inbox.domain_id).where(Domain.domain == domain.lower())
    for name, value in request.query_params.multi_items():
        if name.startswith("metadata."):
            stmt = stmt.where(Inbox.metadata_[name[len("metadata."):]].astext == value)
    rows, next_cursor = await paginate(session, stmt, Inbox.id, params)
    return list_response([inbox_to_dict(i) for i in rows], next_cursor)


@router.get("/{inbox_id}", response_model=InboxOut)
async def get(
    inbox_id: str,
    principal: Principal = Depends(require_scope("inboxes:read")),
    session: AsyncSession = Depends(get_session),
):
    return inbox_to_dict(await get_inbox(session, principal.organization_id, inbox_id))


async def _set_status(session: AsyncSession, principal: Principal, inbox_id: str, new_status: str, event: str):
    inbox = await get_inbox(session, principal.organization_id, inbox_id)
    if inbox.status == "expired":
        raise APIError(409, "inbox_disabled", "Expired inboxes cannot change status.")
    inbox.status = new_status
    await emit(session, organization_id=inbox.organization_id, resource_type="inbox", resource_id=inbox.id,
               type=event, payload={"inbox_id": inbox.id, "inbox": inbox_to_dict(inbox)})
    await session.commit()
    return inbox_to_dict(inbox)


@router.post("/{inbox_id}/disable", response_model=InboxOut)
async def disable(inbox_id: str, principal: Principal = Depends(require_scope("inboxes:write")),
                  session: AsyncSession = Depends(get_session)):
    return await _set_status(session, principal, inbox_id, "suspended", "inbox.disabled")


@router.post("/{inbox_id}/enable", response_model=InboxOut)
async def enable(inbox_id: str, principal: Principal = Depends(require_scope("inboxes:write")),
                 session: AsyncSession = Depends(get_session)):
    return await _set_status(session, principal, inbox_id, "active", "inbox.enabled")


@router.delete("/{inbox_id}", status_code=204, response_class=Response)
async def delete(inbox_id: str, principal: Principal = Depends(require_scope("inboxes:write")),
                 session: AsyncSession = Depends(get_session)):
    inbox = await get_inbox(session, principal.organization_id, inbox_id)
    inbox.status = "deleted"
    inbox.deleted_at = utcnow()
    await emit(session, organization_id=inbox.organization_id, resource_type="inbox", resource_id=inbox.id,
               type="inbox.deleted", payload={"inbox_id": inbox.id})
    await session.commit()
    return Response(status_code=204)
