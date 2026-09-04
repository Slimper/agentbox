import secrets

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import APIError, not_found
from agentbox.api.idempotency import IdempotencyGuard, idempotency
from agentbox.api.pagination import PageParams, list_response, page_params, paginate
from agentbox.api.schemas import DomainCreate
from agentbox.db.models import Domain, Inbox, utcnow
from agentbox.domain.ids import new_id
from agentbox.domains.verify import domain_to_dict
from agentbox.extensions import registry
from agentbox.jobs.queue import enqueue
from agentbox.runtime import Runtime
from agentbox.services.events import emit

router = APIRouter(prefix="/v1/domains", tags=["domains"])


async def _get(session: AsyncSession, org_id: str, domain_id: str) -> Domain:
    d = await session.scalar(
        select(Domain).where(Domain.id == domain_id, Domain.organization_id == org_id, Domain.deleted_at.is_(None))
    )
    if d is None:
        raise not_found("domain", domain_id)
    return d


@router.post("", status_code=201)
async def create(
    body: DomainCreate,
    principal: Principal = Depends(require_scope("domains:write")),
    session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime),
    idem: IdempotencyGuard = Depends(idempotency),
):
    if idem.replay:
        return idem.replay
    existing = await session.scalar(select(Domain).where(Domain.domain == body.domain, Domain.deleted_at.is_(None)))
    if existing is not None:
        if existing.organization_id == principal.organization_id:
            return await idem.commit(200, domain_to_dict(existing, runtime.settings))
        raise APIError(409, "conflict", f"Domain '{body.domain}' is already registered.")
    for hook in registry().hooks(runtime.settings, "domain.before_create"):
        await hook(session, principal.organization_id, runtime.settings)
    d = Domain(id=new_id("dom"), organization_id=principal.organization_id, domain=body.domain,
               type="customer_custom", status="verification_pending",
               verification_token=secrets.token_urlsafe(24))
    session.add(d)
    await session.flush()
    await emit(session, organization_id=d.organization_id, resource_type="domain", resource_id=d.id,
               type="domain.verification_pending", payload={"domain_id": d.id, "domain": domain_to_dict(d)})
    await enqueue(session, "domain_verify", {"domain_id": d.id})
    await session.commit()
    return await idem.commit(201, domain_to_dict(d, runtime.settings))


@router.get("")
async def list_domains(principal: Principal = Depends(require_scope("domains:read")),
                       session: AsyncSession = Depends(get_session), params: PageParams = Depends(page_params),
                       runtime: Runtime = Depends(get_runtime)):
    stmt = select(Domain).where(Domain.organization_id == principal.organization_id, Domain.deleted_at.is_(None))
    rows, next_cursor = await paginate(session, stmt, Domain.id, params)
    return list_response([domain_to_dict(d, runtime.settings) for d in rows], next_cursor)


@router.get("/{domain_id}")
async def get_one(domain_id: str, principal: Principal = Depends(require_scope("domains:read")),
                  session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    return domain_to_dict(await _get(session, principal.organization_id, domain_id), runtime.settings)


@router.post("/{domain_id}/verify", status_code=202)
async def verify_now(domain_id: str, principal: Principal = Depends(require_scope("domains:write")),
                     session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    d = await _get(session, principal.organization_id, domain_id)
    await enqueue(session, "domain_verify", {"domain_id": d.id})
    await session.commit()
    return domain_to_dict(d, runtime.settings)


@router.delete("/{domain_id}", status_code=204, response_class=Response)
async def delete(domain_id: str, principal: Principal = Depends(require_scope("domains:write")),
                 session: AsyncSession = Depends(get_session)):
    d = await _get(session, principal.organization_id, domain_id)
    active = await session.scalar(
        select(func.count()).select_from(Inbox).where(Inbox.domain_id == d.id, Inbox.deleted_at.is_(None))
    )
    if active:
        raise APIError(409, "conflict", f"Domain has {active} inbox(es); delete them first.")
    d.deleted_at = utcnow()
    d.status = "deleted"
    await emit(session, organization_id=d.organization_id, resource_type="domain", resource_id=d.id,
               type="domain.deleted", payload={"domain_id": d.id})
    await session.commit()
    return Response(status_code=204)
