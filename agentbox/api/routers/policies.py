from fastapi import APIRouter, Body, Depends
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.api.errors import APIError
from agentbox.governance.policies import (
    DEFAULT_POLICY,
    get_effective_policy,
    get_policy_row,
    set_policy,
    validate_policy_config,
)
from agentbox.services.events import emit
from agentbox.services.inboxes import get_inbox

router = APIRouter(prefix="/v1", tags=["policies"])


async def _view(session: AsyncSession, org_id: str, inbox_id: str | None) -> dict:
    row = await get_policy_row(session, org_id, inbox_id)
    return {"scope": "inbox" if inbox_id else "organization", "inbox_id": inbox_id,
            "config": row.config if row else {}, "effective": await get_effective_policy(session, org_id, inbox_id),
            "defaults": DEFAULT_POLICY}


async def _put(session: AsyncSession, principal: Principal, inbox_id: str | None, body: dict) -> dict:
    try:
        cfg = validate_policy_config(body)
    except ValidationError as e:
        raise APIError(422, "validation_error", "Invalid policy config.", {"errors": e.errors()}) from e
    await set_policy(session, principal.organization_id, inbox_id, cfg)
    await emit(session, organization_id=principal.organization_id, resource_type="policy",
               resource_id=inbox_id or principal.organization_id, type="policy.changed",
               payload={"inbox_id": inbox_id, "config": cfg, "actor": principal.api_key_id})
    await session.commit()
    return await _view(session, principal.organization_id, inbox_id)


@router.get("/policy")
async def get_org_policy(principal: Principal = Depends(require_scope("policies:read")),
                         session: AsyncSession = Depends(get_session)):
    return await _view(session, principal.organization_id, None)


@router.put("/policy")
async def put_org_policy(body: dict = Body(...), principal: Principal = Depends(require_scope("policies:write")),
                         session: AsyncSession = Depends(get_session)):
    return await _put(session, principal, None, body)


@router.delete("/policy", status_code=204, response_class=Response)
async def delete_org_policy(principal: Principal = Depends(require_scope("policies:write")),
                            session: AsyncSession = Depends(get_session)):
    row = await get_policy_row(session, principal.organization_id, None)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return Response(status_code=204)


@router.get("/inboxes/{inbox_id}/policy")
async def get_inbox_policy(inbox_id: str, principal: Principal = Depends(require_scope("policies:read")),
                           session: AsyncSession = Depends(get_session)):
    await get_inbox(session, principal.organization_id, inbox_id)
    return await _view(session, principal.organization_id, inbox_id)


@router.put("/inboxes/{inbox_id}/policy")
async def put_inbox_policy(inbox_id: str, body: dict = Body(...),
                           principal: Principal = Depends(require_scope("policies:write")),
                           session: AsyncSession = Depends(get_session)):
    await get_inbox(session, principal.organization_id, inbox_id)
    return await _put(session, principal, inbox_id, body)


@router.delete("/inboxes/{inbox_id}/policy", status_code=204, response_class=Response)
async def delete_inbox_policy(inbox_id: str, principal: Principal = Depends(require_scope("policies:write")),
                              session: AsyncSession = Depends(get_session)):
    await get_inbox(session, principal.organization_id, inbox_id)
    row = await get_policy_row(session, principal.organization_id, inbox_id)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return Response(status_code=204)
