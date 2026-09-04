from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.api.errors import APIError, not_found
from agentbox.api.pagination import PageParams, list_response, page_params, paginate
from agentbox.api.schemas import SuppressionCreate
from agentbox.db.models import Suppression
from agentbox.governance.suppressions import add_suppression, suppression_to_dict
from agentbox.services.events import emit

router = APIRouter(prefix="/v1/suppressions", tags=["suppressions"])


@router.get("")
async def list_suppressions(email: str | None = None, reason: str | None = None,
                            principal: Principal = Depends(require_scope("suppressions:read")),
                            session: AsyncSession = Depends(get_session), params: PageParams = Depends(page_params)):
    stmt = select(Suppression).where(Suppression.organization_id == principal.organization_id)
    if email:
        stmt = stmt.where(Suppression.email == email.lower())
    if reason:
        stmt = stmt.where(Suppression.reason == reason)
    rows, next_cursor = await paginate(session, stmt, Suppression.id, params)
    return list_response([suppression_to_dict(s) for s in rows], next_cursor)


@router.post("", status_code=201)
async def create(body: SuppressionCreate, principal: Principal = Depends(require_scope("suppressions:write")),
                 session: AsyncSession = Depends(get_session)):
    expires = None
    if body.expires_at:
        try:
            expires = datetime.fromisoformat(body.expires_at)
        except ValueError as e:
            raise APIError(422, "validation_error", "expires_at must be ISO 8601.") from e
    row = await add_suppression(session, organization_id=principal.organization_id, email=body.email,
                                reason=body.reason, source="manual", note=body.note, expires_at=expires)
    await session.commit()
    return suppression_to_dict(row)


@router.delete("/{suppression_id}", status_code=204, response_class=Response)
async def delete(suppression_id: str, principal: Principal = Depends(require_scope("suppressions:write")),
                 session: AsyncSession = Depends(get_session)):
    row = await session.scalar(select(Suppression).where(Suppression.id == suppression_id,
                                                         Suppression.organization_id == principal.organization_id))
    if row is None:
        raise not_found("suppression", suppression_id)
    await emit(session, organization_id=principal.organization_id, resource_type="suppression", resource_id=row.id,
               type="suppression.removed", payload={"email": row.email, "actor": principal.api_key_id})
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)
