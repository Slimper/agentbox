from datetime import timedelta

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Domain, Job, utcnow
from agentbox.domains.dns import check_domain
from agentbox.jobs.queue import enqueue
from agentbox.jobs.worker import JobContext
from agentbox.services.events import emit

log = structlog.get_logger("agentbox.domains")


def domain_to_dict(d: Domain, settings=None) -> dict:
    out = {
        "id": d.id, "domain": d.domain, "type": d.type, "status": d.status,
        "inbound_status": d.inbound_status, "outbound_status": d.outbound_status,
        "spf_status": d.spf_status, "dkim_status": d.dkim_status, "dmarc_status": d.dmarc_status,
        "mx_status": d.mx_status, "check_results": d.check_results,
        "verified_at": d.verified_at.isoformat() if d.verified_at else None,
        "last_checked_at": d.last_checked_at.isoformat() if d.last_checked_at else None,
        "next_check_at": d.next_check_at.isoformat() if d.next_check_at else None,
        "created_at": d.created_at.isoformat(),
    }
    if settings is not None and d.type == "customer_custom" and d.verification_token:
        from agentbox.domains.dns import expected_records

        out["dns"] = expected_records(d.domain, d.verification_token, settings)
    return out


def apply_check_results(domain: Domain, results: dict) -> str | None:
    """Update status columns from a check; return the event type to emit (or None)."""
    domain.mx_status = results.get("mx", "unknown")
    domain.spf_status = results.get("spf", "unknown")
    domain.dmarc_status = results.get("dmarc", "unknown")
    domain.dkim_status = results.get("dkim", "unknown")
    domain.check_results = results
    domain.last_checked_at = utcnow()
    minimum_ok = results.get("ownership") == "ok" and results.get("mx") in ("ok", "partial")
    domain.inbound_status = "active" if minimum_ok else "unverified"
    domain.outbound_status = "active" if results.get("ownership") == "ok" else "unverified"
    previous = domain.status
    if minimum_ok:
        domain.status = "active"
        if previous != "active":
            domain.verified_at = domain.verified_at or utcnow()
            return "domain.verified"
        return None
    if previous == "active":
        domain.status = "degraded"
        return "domain.degraded"
    if previous == "degraded" and not minimum_ok:
        return None
    domain.status = "verification_pending"
    return None


async def verify_domain(ctx: JobContext, session: AsyncSession) -> None:
    domain = await session.scalar(
        select(Domain).where(Domain.id == ctx.payload["domain_id"]).with_for_update(key_share=True)
    )
    if domain is None or domain.deleted_at is not None or domain.type != "customer_custom":
        return
    settings = ctx.runtime.settings
    results = await check_domain(ctx.runtime.dns, domain.domain, domain.verification_token or "", settings)
    event_type = apply_check_results(domain, results)
    if event_type:
        await emit(session, organization_id=domain.organization_id, resource_type="domain", resource_id=domain.id,
                   type=event_type, payload={"domain_id": domain.id, "domain": domain_to_dict(domain)})
    interval = settings.domain_recheck_active_seconds if domain.status == "active" \
        else settings.domain_recheck_pending_seconds
    domain.next_check_at = utcnow() + timedelta(seconds=interval)
    await session.execute(
        delete(Job).where(Job.kind == "domain_verify", Job.status == "pending",
                          Job.payload["domain_id"].astext == domain.id)
    )
    await enqueue(session, "domain_verify", {"domain_id": domain.id, "scheduled": True}, run_at=domain.next_check_at)
    log.info("domain_checked", domain=domain.domain, status=domain.status, results=results)
