from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import (
    Attachment,
    Domain,
    Inbox,
    Message,
    Organization,
    UsageDaily,
    WebhookDelivery,
    utcnow,
)
from agentbox.jobs.worker import JobContext


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


async def compute_usage(session: AsyncSession, organization_id: str, day: date) -> dict:
    start, end = day_bounds(day)
    org = Inbox.organization_id == organization_id

    active_inboxes = await session.scalar(
        select(func.count()).select_from(Inbox).where(
            org, Inbox.created_at < end, or_(Inbox.deleted_at.is_(None), Inbox.deleted_at >= end),
            Inbox.status.in_(["active", "suspended"]))
    )
    ephemeral = await session.scalar(
        select(func.count()).select_from(Inbox).where(org, Inbox.expires_at.is_not(None),
                                                      Inbox.created_at >= start, Inbox.created_at < end)
    )
    sent = await session.scalar(
        select(func.count()).select_from(Message).where(
            Message.organization_id == organization_id, Message.direction == "outbound",
            Message.created_at >= start, Message.created_at < end,
            Message.status.not_in(["rejected", "pending_approval"]))
    )
    received = await session.scalar(
        select(func.count()).select_from(Message).where(
            Message.organization_id == organization_id, Message.direction == "inbound",
            Message.created_at >= start, Message.created_at < end)
    )
    stored = await session.scalar(
        select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(
            Attachment.organization_id == organization_id, Attachment.created_at < end, Attachment.status == "ready")
    )
    webhook_attempts = await session.scalar(
        select(func.count()).select_from(WebhookDelivery).where(
            WebhookDelivery.organization_id == organization_id, WebhookDelivery.created_at >= start,
            WebhookDelivery.created_at < end)
    )
    custom_domains = await session.scalar(
        select(func.count()).select_from(Domain).where(
            Domain.organization_id == organization_id, Domain.type == "customer_custom", Domain.created_at < end,
            or_(Domain.deleted_at.is_(None), Domain.deleted_at >= end))
    )
    return {
        "day": day.isoformat(), "active_inboxes": int(active_inboxes or 0),
        "ephemeral_inboxes_created": int(ephemeral or 0), "messages_sent": int(sent or 0),
        "messages_received": int(received or 0), "attachment_bytes_stored": int(stored or 0),
        "webhook_attempts": int(webhook_attempts or 0), "custom_domains": int(custom_domains or 0),
    }


async def upsert_usage(session: AsyncSession, organization_id: str, day: date) -> UsageDaily:
    values = await compute_usage(session, organization_id, day)
    row = await session.scalar(select(UsageDaily).where(UsageDaily.organization_id == organization_id,
                                                        UsageDaily.day == day))
    if row is None:
        row = UsageDaily(organization_id=organization_id, day=day)
        session.add(row)
    for k, v in values.items():
        if k != "day":
            setattr(row, k, v)
    row.computed_at = utcnow()
    await session.flush()
    return row


async def rollup_usage(ctx: JobContext, session: AsyncSession) -> None:
    """Recompute immutable-ish daily usage rows for every organization (today and yesterday by default)."""
    days = ctx.payload.get("days")
    if days:
        targets = [date.fromisoformat(d) for d in days]
    else:
        today = utcnow().date()
        targets = [today - timedelta(days=1), today]
    org_ids = list(await session.scalars(select(Organization.id).where(Organization.status == "active")))
    for org_id in org_ids:
        for day in targets:
            await upsert_usage(session, org_id, day)


def usage_to_dict(u: UsageDaily) -> dict:
    return {"day": u.day.isoformat(), "active_inboxes": u.active_inboxes,
            "ephemeral_inboxes_created": u.ephemeral_inboxes_created, "messages_sent": u.messages_sent,
            "messages_received": u.messages_received, "attachment_bytes_stored": u.attachment_bytes_stored,
            "webhook_attempts": u.webhook_attempts, "custom_domains": u.custom_domains,
            "computed_at": u.computed_at.isoformat()}

