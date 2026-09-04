from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.db.models import UsageDaily, utcnow
from agentbox.usage.rollup import compute_usage, usage_to_dict

router = APIRouter(prefix="/v1/usage", tags=["usage"])

TOTAL_KEYS = ("messages_sent", "messages_received", "webhook_attempts", "ephemeral_inboxes_created")


@router.get("")
async def usage(since: date | None = None, until: date | None = None,
                principal: Principal = Depends(require_scope("usage:read")),
                session: AsyncSession = Depends(get_session)):
    until = until or utcnow().date()
    since = since or until - timedelta(days=30)
    rows = list(await session.scalars(
        select(UsageDaily).where(UsageDaily.organization_id == principal.organization_id, UsageDaily.day >= since,
                                 UsageDaily.day <= until).order_by(UsageDaily.day)
    ))
    totals = {k: sum(getattr(r, k) for r in rows) for k in TOTAL_KEYS}
    last = rows[-1] if rows else None
    return {"since": since.isoformat(), "until": until.isoformat(), "data": [usage_to_dict(r) for r in rows],
            "totals": totals,
            "latest": {"active_inboxes": last.active_inboxes if last else 0,
                       "attachment_bytes_stored": last.attachment_bytes_stored if last else 0,
                       "custom_domains": last.custom_domains if last else 0}}


@router.get("/current")
async def current(principal: Principal = Depends(require_scope("usage:read")),
                  session: AsyncSession = Depends(get_session)):
    """Live counters for today, computed on demand (not yet rolled up)."""
    return await compute_usage(session, principal.organization_id, utcnow().date())
