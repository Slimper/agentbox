from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.api.pagination import PageParams, list_response, page_params, paginate
from agentbox.db.models import Event
from agentbox.services.events import event_to_dict

router = APIRouter(prefix="/v1/events", tags=["events"])


@router.get("")
async def list_events(
    type: str | None = None, resource_id: str | None = None,
    since: datetime | None = None, until: datetime | None = None,
    principal: Principal = Depends(require_scope("events:read")),
    session: AsyncSession = Depends(get_session), params: PageParams = Depends(page_params),
):
    stmt = select(Event).where(Event.organization_id == principal.organization_id)
    if type:
        stmt = stmt.where(Event.type == type)
    if resource_id:
        stmt = stmt.where(Event.resource_id == resource_id)
    if since:
        stmt = stmt.where(Event.created_at >= since)
    if until:
        stmt = stmt.where(Event.created_at < until)
    rows, next_cursor = await paginate(session, stmt, Event.id, params)
    return list_response([event_to_dict(e) for e in rows], next_cursor)


@router.get("/export")
async def export_events(
    since: datetime | None = None, until: datetime | None = None, type: str | None = None,
    principal: Principal = Depends(require_scope("events:read")), session: AsyncSession = Depends(get_session),
):
    """Newline-delimited JSON stream of events for SIEM / archive ingestion (oldest first, up to 100 000 rows)."""
    import json

    from fastapi.responses import StreamingResponse

    stmt = select(Event).where(Event.organization_id == principal.organization_id)
    if since:
        stmt = stmt.where(Event.created_at >= since)
    if until:
        stmt = stmt.where(Event.created_at < until)
    if type:
        stmt = stmt.where(Event.type == type)
    rows = await session.scalars(stmt.order_by(Event.id).limit(100_000))

    async def gen():
        for e in rows:
            yield json.dumps(event_to_dict(e), ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="agentbox-events.ndjson"'})
