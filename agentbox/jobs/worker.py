import asyncio
import contextlib
import socket
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Job, utcnow
from agentbox.jobs.queue import RetryLater, backoff_for, claim, ensure_periodic_jobs, mark_dead, mark_done, reschedule
from agentbox.runtime import Runtime

log = structlog.get_logger("agentbox.jobs")

STALE_LOCK = timedelta(minutes=10)
DONE_RETENTION = timedelta(days=7)


@dataclass
class JobContext:
    runtime: Runtime
    job_id: int
    kind: str
    payload: dict
    attempts: int
    max_attempts: int

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


Handler = Callable[[JobContext, AsyncSession], Awaitable[None]]


class JobWorker:
    def __init__(
        self,
        runtime: Runtime,
        handlers: dict[str, Handler],
        kinds: list[str] | None = None,
        concurrency: int = 4,
        poll_interval: float = 0.5,
        worker_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.handlers = handlers
        self.kinds = kinds or list(handlers)
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.worker_id = worker_id or f"{socket.gethostname()}-{id(self)}"

    async def run(self, stop: asyncio.Event) -> None:
        loops = [asyncio.create_task(self._loop(stop, i)) for i in range(self.concurrency)]
        sweeper = asyncio.create_task(self._sweeper(stop))
        await stop.wait()
        for t in loops + [sweeper]:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _loop(self, stop: asyncio.Event, n: int) -> None:
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except Exception:  # noqa: BLE001
                log.error("worker_loop_error", worker=self.worker_id, n=n, exc=traceback.format_exc())
                processed = False
            if not processed:
                await asyncio.sleep(self.poll_interval)

    async def _sweeper(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001
                log.error("sweeper_error", exc=traceback.format_exc())
            await asyncio.sleep(60)

    async def sweep(self) -> None:
        async with self.runtime.db.session() as s:
            await s.execute(
                update(Job).where(Job.status == "running", Job.locked_at < utcnow() - STALE_LOCK)
                .values(status="pending", locked_at=None, locked_by=None, updated_at=utcnow())
            )
            await s.execute(delete(Job).where(Job.status == "done", Job.updated_at < utcnow() - DONE_RETENTION))
            await ensure_periodic_jobs(s, settings=self.runtime.settings)
            await s.commit()

    async def drain(self, max_jobs: int = 100) -> int:
        n = 0
        while n < max_jobs and await self.run_once():
            n += 1
        return n

    async def run_once(self) -> bool:
        async with self.runtime.db.session() as s:
            job = await claim(s, self.worker_id, self.kinds)
            await s.commit()
        if job is None:
            return False
        ctx = JobContext(self.runtime, job.id, job.kind, job.payload, job.attempts, job.max_attempts)
        handler = self.handlers.get(job.kind)
        if handler is None:
            async with self.runtime.db.session() as s:
                await mark_dead(s, job.id, f"no handler for kind {job.kind}")
                await s.commit()
            return True
        try:
            async with self.runtime.db.session() as s:
                await handler(ctx, s)
                await mark_done(s, job.id)
                await s.commit()
            log.info("job_done", job_id=job.id, kind=job.kind, attempts=job.attempts)
        except RetryLater as e:
            await self._retry(ctx, e.delay_seconds, e.error or str(e))
        except Exception as e:  # noqa: BLE001
            log.error("job_failed", job_id=job.id, kind=job.kind, attempts=job.attempts, exc=traceback.format_exc())
            await self._retry(ctx, backoff_for(job.kind, job.attempts), f"{type(e).__name__}: {e}")
        return True

    async def _retry(self, ctx: JobContext, delay: int, error: str) -> None:
        async with self.runtime.db.session() as s:
            if ctx.is_last_attempt:
                await mark_dead(s, ctx.job_id, error)
            else:
                await reschedule(s, ctx.job_id, delay, error)
            await s.commit()
