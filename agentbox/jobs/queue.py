from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Job, utcnow

BACKOFF: dict[str, list[int]] = {
    "outbound_send": [30, 120, 600, 3600, 14400],
    "inbound_process": [10, 60, 300, 1800, 7200],
    "webhook_deliver": [10, 60, 300, 1800, 7200, 28800, 86400],
    "inbox_expire": [60, 600],
    "domain_verify": [60, 300, 900],
    "usage_rollup": [300, 1800],
    "connector_sync": [60, 300],
}


def _delays(kind: str) -> list[int]:
    if kind in BACKOFF:
        return BACKOFF[kind]
    from agentbox.extensions import registry

    delays = registry().backoff().get(kind)
    if delays is None:
        raise KeyError(f"unknown job kind {kind!r}")
    return delays


def max_attempts_for(kind: str) -> int:
    return 1 + len(_delays(kind))


def backoff_for(kind: str, attempts: int) -> int:
    delays = _delays(kind)
    return delays[min(max(attempts, 1), len(delays)) - 1]


class RetryLater(Exception):
    def __init__(self, delay_seconds: int, error: str = "") -> None:
        super().__init__(error or f"retry in {delay_seconds}s")
        self.delay_seconds = delay_seconds
        self.error = error


async def enqueue(session: AsyncSession, kind: str, payload: dict, *, run_at: datetime | None = None) -> Job:
    try:
        _delays(kind)
    except KeyError as e:
        raise ValueError(f"unknown job kind: {kind}") from e
    job = Job(kind=kind, payload=payload, run_at=run_at or utcnow(), max_attempts=max_attempts_for(kind))
    session.add(job)
    await session.flush()
    return job


async def claim(session: AsyncSession, worker_id: str, kinds: list[str] | None = None) -> Job | None:
    candidate = (
        select(Job.id)
        .where(Job.status == "pending", Job.run_at <= func.now())
        .order_by(Job.run_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds:
        candidate = candidate.where(Job.kind.in_(kinds))
    stmt = (
        update(Job)
        .where(Job.id == candidate.scalar_subquery())
        .values(status="running", locked_at=func.now(), locked_by=worker_id,
                attempts=Job.attempts + 1, updated_at=func.now())
        .returning(Job.id)
    )
    job_id = await session.scalar(stmt)
    if job_id is None:
        return None
    return await session.get(Job, job_id, populate_existing=True)


async def reschedule(session: AsyncSession, job_id: int, delay_seconds: int, error: str | None) -> None:
    await session.execute(
        update(Job).where(Job.id == job_id).values(
            status="pending", run_at=utcnow() + timedelta(seconds=delay_seconds),
            locked_at=None, locked_by=None, last_error=error, updated_at=utcnow(),
        )
    )


async def mark_dead(session: AsyncSession, job_id: int, error: str | None) -> None:
    await session.execute(
        update(Job).where(Job.id == job_id).values(
            status="dead", locked_at=None, locked_by=None, last_error=error, updated_at=utcnow()
        )
    )


async def mark_done(session: AsyncSession, job_id: int) -> None:
    await session.execute(
        update(Job).where(Job.id == job_id).values(status="done", locked_at=None, updated_at=utcnow())
    )


PERIODIC: dict[str, timedelta] = {"usage_rollup": timedelta(hours=1), "connector_sync": timedelta(minutes=2)}


def periodic_jobs(settings=None) -> dict[str, timedelta]:
    """Core periodic kinds plus those of the extensions active for `settings`."""
    from agentbox.extensions import registry

    return {**PERIODIC, **registry().periodic(settings)}


async def ensure_periodic_jobs(session: AsyncSession, kinds: list[str] | None = None, settings=None) -> None:
    """Enqueue recurring jobs (usage rollup, extension jobs) unless one was created within its period."""
    for kind, period in periodic_jobs(settings).items():
        if kinds is not None and kind not in kinds:
            continue
        recent = await session.scalar(
            select(Job.id).where(Job.kind == kind, Job.created_at >= utcnow() - period).limit(1)
        )
        if recent is None:
            await enqueue(session, kind, {})
