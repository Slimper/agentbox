from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.db.models import Message, utcnow

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])

STATUSES = ["queued", "provider_accepted", "delivered", "deferred", "bounced", "rejected", "complained", "failed",
            "pending_approval"]


@router.get("/delivery")
async def delivery(
    since: datetime | None = None, until: datetime | None = None,
    group_by: str = Query("provider", pattern="^(provider|recipient_domain|inbox|day)$"),
    principal: Principal = Depends(require_scope("analytics:read")), session: AsyncSession = Depends(get_session),
):
    since = since or utcnow() - timedelta(days=30)
    until = until or utcnow()
    if group_by == "provider":
        key = func.coalesce(Message.provider, "unassigned")
    elif group_by == "recipient_domain":
        key = func.split_part(Message.to_addresses[0]["email"].astext, "@", 2)
    elif group_by == "inbox":
        key = Message.inbox_id
    else:
        key = func.to_char(Message.created_at, "YYYY-MM-DD")
    stmt = (
        select(key.label("key"), Message.status, func.count().label("n"))
        .where(Message.organization_id == principal.organization_id, Message.direction == "outbound",
               Message.created_at >= since, Message.created_at < until)
        .group_by(key, Message.status)
    )
    rows: dict[str, dict] = {}
    for k, status, n in await session.execute(stmt):
        bucket = rows.setdefault(k or "", {"key": k or "", "sent": 0, **{s: 0 for s in STATUSES}})
        bucket["sent"] += n
        if status in bucket:
            bucket[status] += n
    for b in rows.values():
        delivered_like = b["provider_accepted"] + b["delivered"] + b["deferred"]
        b["delivery_rate"] = round(delivered_like / b["sent"], 4) if b["sent"] else None
        b["bounce_rate"] = round(b["bounced"] / b["sent"], 4) if b["sent"] else None
    return {"since": since.isoformat(), "until": until.isoformat(), "group_by": group_by,
            "data": sorted(rows.values(), key=lambda b: -b["sent"])}
