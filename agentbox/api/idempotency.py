import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from agentbox.api.auth import Principal, authenticate
from agentbox.api.deps import get_runtime
from agentbox.api.errors import APIError
from agentbox.db.models import IdempotencyKey, utcnow
from agentbox.runtime import Runtime

HEADER = "Idempotency-Key"
IN_FLIGHT_WAIT_SECONDS = 5.0


def request_hash(body: bytes) -> str:
    try:
        canonical = json.dumps(json.loads(body or b"{}"), sort_keys=True, separators=(",", ":")).encode()
    except ValueError:
        canonical = body
    return hashlib.sha256(canonical).hexdigest()


@dataclass
class IdempotencyGuard:
    runtime: Runtime | None = None
    organization_id: str = ""
    key: str | None = None
    endpoint: str = ""
    replay: JSONResponse | None = None
    committed: bool = field(default=False)

    @property
    def active(self) -> bool:
        return self.key is not None

    async def commit(self, status: int, body: dict) -> JSONResponse:
        if self.active:
            async with self.runtime.db.session() as s:
                row = await s.get(IdempotencyKey, (self.organization_id, self.key, self.endpoint))
                if row is not None:
                    row.response_status = status
                    row.response_body = body
                    await s.commit()
            self.committed = True
        return JSONResponse(status_code=status, content=body)

    async def discard(self) -> None:
        if self.active and not self.committed:
            async with self.runtime.db.session() as s:
                await s.execute(
                    delete(IdempotencyKey).where(
                        IdempotencyKey.organization_id == self.organization_id,
                        IdempotencyKey.key == self.key, IdempotencyKey.endpoint == self.endpoint,
                    )
                )
                await s.commit()


async def _begin(runtime: Runtime, org_id: str, key: str, endpoint: str, digest: str) -> IdempotencyGuard:
    guard = IdempotencyGuard(runtime=runtime, organization_id=org_id, key=key, endpoint=endpoint)
    ttl = timedelta(seconds=runtime.settings.idempotency_ttl_seconds)
    deadline = asyncio.get_running_loop().time() + IN_FLIGHT_WAIT_SECONDS
    while True:
        async with runtime.db.session() as s:
            existing = await s.scalar(
                select(IdempotencyKey).where(
                    IdempotencyKey.organization_id == org_id, IdempotencyKey.key == key,
                    IdempotencyKey.endpoint == endpoint,
                ).execution_options(populate_existing=True)
            )
            if existing is not None and existing.expires_at < utcnow():
                await s.delete(existing)
                await s.commit()
                existing = None
            if existing is None:
                s.add(IdempotencyKey(organization_id=org_id, key=key, endpoint=endpoint,
                                     request_hash=digest, expires_at=utcnow() + ttl))
                try:
                    await s.commit()
                    return guard
                except IntegrityError:
                    await s.rollback()
                    continue
            if existing.request_hash != digest:
                raise APIError(409, "idempotency_conflict",
                               "Idempotency-Key was already used with a different request body.")
            if existing.response_status is not None:
                guard.replay = JSONResponse(status_code=existing.response_status, content=existing.response_body,
                                            headers={"Idempotent-Replayed": "true"})
                guard.committed = True
                return guard
        if asyncio.get_running_loop().time() > deadline:
            raise APIError(409, "conflict", "A request with this Idempotency-Key is still in progress.")
        await asyncio.sleep(0.1)


async def idempotency(
    request: Request, principal: Principal = Depends(authenticate), runtime: Runtime = Depends(get_runtime)
) -> AsyncIterator[IdempotencyGuard]:
    key = request.headers.get(HEADER)
    if not key:
        yield IdempotencyGuard()
        return
    if len(key) > 255:
        raise APIError(422, "validation_error", "Idempotency-Key must be at most 255 characters.")
    endpoint = f"{request.method} {request.url.path}"
    guard = await _begin(runtime, principal.organization_id, key.strip(), endpoint, request_hash(await request.body()))
    try:
        yield guard
    except Exception:
        await guard.discard()
        raise
    else:
        if guard.active and not guard.committed:
            await guard.discard()
