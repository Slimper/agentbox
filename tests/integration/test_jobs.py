from datetime import timedelta

from sqlalchemy import select

from agentbox.db.models import Job, utcnow
from agentbox.jobs.queue import RetryLater, enqueue
from agentbox.jobs.worker import JobWorker


async def test_enqueue_claim_done(runtime):
    seen = []

    async def handler(ctx, session):
        seen.append(ctx.payload["x"])

    async with runtime.db.session() as s:
        await enqueue(s, "inbox_expire", {"x": 1})
        await enqueue(s, "inbox_expire", {"x": 2}, run_at=utcnow() + timedelta(hours=1))
        await s.commit()

    worker = JobWorker(runtime, {"inbox_expire": handler}, concurrency=1)
    assert await worker.drain() == 1
    assert seen == [1]
    async with runtime.db.session() as s:
        jobs = (await s.scalars(select(Job).order_by(Job.id))).all()
        assert [j.status for j in jobs] == ["done", "pending"]


async def test_exception_reschedules_then_dead(runtime):
    async def boom(ctx, session):
        raise RuntimeError("nope")

    async with runtime.db.session() as s:
        job = await enqueue(s, "inbox_expire", {})
        await s.commit()
    worker = JobWorker(runtime, {"inbox_expire": boom}, concurrency=1)
    assert await worker.run_once() is True
    async with runtime.db.session() as s:
        j = await s.get(Job, job.id)
        assert j.status == "pending" and j.attempts == 1 and "nope" in j.last_error
        assert j.run_at > utcnow() + timedelta(seconds=50)
        j.run_at = utcnow()
        await s.commit()
    assert await worker.run_once() is True
    async with runtime.db.session() as s:
        j = await s.get(Job, job.id)
        assert j.status == "pending" and j.attempts == 2
        j.run_at = utcnow()
        await s.commit()
    assert await worker.run_once() is True
    async with runtime.db.session() as s:
        j = await s.get(Job, job.id)
        assert j.status == "dead" and j.attempts == 3


async def test_retry_later_uses_explicit_delay_and_handler_changes_roll_back(runtime):
    async def later(ctx, session):
        await enqueue(session, "inbox_expire", {"child": True})
        raise RetryLater(5, "wait")

    async with runtime.db.session() as s:
        await enqueue(s, "inbox_expire", {})
        await s.commit()
    worker = JobWorker(runtime, {"inbox_expire": later}, concurrency=1)
    await worker.run_once()
    async with runtime.db.session() as s:
        jobs = (await s.scalars(select(Job))).all()
        assert len(jobs) == 1  # child enqueue rolled back with the failed handler
        j = jobs[0]
        assert j.status == "pending" and j.last_error == "wait"
        assert timedelta(seconds=3) < j.run_at - utcnow() <= timedelta(seconds=5)


async def test_sweeper_reclaims_stale_running(runtime):
    async with runtime.db.session() as s:
        job = await enqueue(s, "inbox_expire", {})
        job.status = "running"
        job.locked_at = utcnow() - timedelta(minutes=11)
        await s.commit()
    worker = JobWorker(runtime, {}, concurrency=1)
    await worker.sweep()
    async with runtime.db.session() as s:
        assert (await s.get(Job, job.id)).status == "pending"
