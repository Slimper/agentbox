from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Suppression
from agentbox.domain.ids import new_id
from agentbox.services.events import emit

REASONS = ("hard_bounce", "complaint", "manual", "policy", "invalid", "abuse")


def suppression_to_dict(s: Suppression) -> dict:
    return {"id": s.id, "email": s.email, "reason": s.reason, "source": s.source, "provider": s.provider,
            "note": s.note, "created_at": s.created_at.isoformat(),
            "expires_at": s.expires_at.isoformat() if s.expires_at else None}


async def add_suppression(
    session: AsyncSession, *, organization_id: str, email: str, reason: str, source: str = "manual",
    provider: str | None = None, note: str | None = None, expires_at: datetime | None = None,
) -> Suppression:
    email = email.strip().lower()
    row = await session.scalar(
        select(Suppression).where(Suppression.organization_id == organization_id, Suppression.email == email)
    )
    if row is not None:
        row.reason, row.source, row.provider, row.note, row.expires_at = reason, source, provider, note, expires_at
        await session.flush()
        return row
    row = Suppression(id=new_id("sup"), organization_id=organization_id, email=email, reason=reason, source=source,
                      provider=provider, note=note, expires_at=expires_at)
    session.add(row)
    await session.flush()
    await emit(session, organization_id=organization_id, resource_type="suppression", resource_id=row.id,
               type="suppression.created", payload={"suppression": suppression_to_dict(row)})
    return row
