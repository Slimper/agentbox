from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.api.errors import APIError, not_found
from agentbox.api.schemas import Model
from agentbox.db.models import ApiKey, utcnow
from agentbox.services.events import emit
from agentbox.services.organizations import ALL_SCOPES, create_api_key

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


class ApiKeyCreate(Model):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ["admin"])
    environment: str = Field(default="live", pattern="^(live|test)$")


def key_to_dict(k: ApiKey) -> dict:
    return {"id": k.id, "name": k.name, "key_prefix": k.key_prefix, "scopes": k.scopes, "environment": k.environment,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None, "created_at": k.created_at.isoformat()}


@router.get("")
async def list_keys(principal: Principal = Depends(require_scope("keys:read")),
                    session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(ApiKey).where(ApiKey.organization_id == principal.organization_id)
                                 .order_by(ApiKey.created_at))
    return {"data": [key_to_dict(k) for k in rows], "next_cursor": None, "scopes": list(ALL_SCOPES)}


@router.post("", status_code=201)
async def create_key(body: ApiKeyCreate, principal: Principal = Depends(require_scope("keys:write")),
                     session: AsyncSession = Depends(get_session)):
    bad = [s for s in body.scopes if s not in ALL_SCOPES]
    if bad:
        raise APIError(422, "validation_error", f"Unknown scopes: {bad}", {"allowed": list(ALL_SCOPES)})
    if not principal.has("admin") and any(not principal.has(s) for s in body.scopes):
        raise APIError(403, "forbidden", "Cannot grant scopes you do not hold.")
    key, plaintext = await create_api_key(session, principal.organization_id, name=body.name,
                                          scopes=tuple(body.scopes), environment=body.environment)
    await emit(session, organization_id=principal.organization_id, resource_type="api_key", resource_id=key.id,
               type="api_key.created", payload={"name": key.name, "scopes": key.scopes, "actor": principal.api_key_id})
    await session.commit()
    return {**key_to_dict(key), "api_key": plaintext}


@router.delete("/{key_id}", status_code=204, response_class=Response)
async def revoke_key(key_id: str, principal: Principal = Depends(require_scope("keys:write")),
                     session: AsyncSession = Depends(get_session)):
    key = await session.scalar(select(ApiKey).where(ApiKey.id == key_id,
                                                    ApiKey.organization_id == principal.organization_id))
    if key is None:
        raise not_found("api_key", key_id)
    if key.id == principal.api_key_id:
        raise APIError(409, "conflict", "Cannot revoke the key used for this request.")
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        await emit(session, organization_id=principal.organization_id, resource_type="api_key", resource_id=key.id,
                   type="api_key.revoked", payload={"name": key.name, "actor": principal.api_key_id})
        await session.commit()
    return Response(status_code=204)
