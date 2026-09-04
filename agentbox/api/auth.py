import hashlib
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.deps import get_session
from agentbox.api.errors import APIError
from agentbox.db.models import ApiKey, Organization, utcnow

KEY_PREFIX_LEN = 12

_WINDOWS: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))


def check_rate_limit(key_id: str, limit_per_minute: int) -> int | None:
    """Fixed one-minute window per API key, per process. Returns seconds to wait when limited."""
    if limit_per_minute <= 0:
        return None
    now = int(time.time())
    window = now // 60
    current_window, count = _WINDOWS[key_id]
    if current_window != window:
        current_window, count = window, 0
    if count >= limit_per_minute:
        return 60 - (now % 60)
    _WINDOWS[key_id] = (current_window, count + 1)
    return None


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key(environment: str = "live") -> tuple[str, str, str]:
    plaintext = f"ab_{environment}_{secrets.token_urlsafe(32)}"
    return plaintext, plaintext[:KEY_PREFIX_LEN], hash_api_key(plaintext)


@dataclass(frozen=True)
class Principal:
    organization_id: str
    api_key_id: str
    scopes: frozenset[str]
    environment: str

    def has(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes


async def principal_for_token(session: AsyncSession, token: str, *, rate_limit_per_minute: int = 0) -> Principal:
    key = await session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(token.strip()), ApiKey.revoked_at.is_(None))
    )
    if key is None:
        raise APIError(401, "unauthorized", "Invalid API key.")
    org = await session.get(Organization, key.organization_id)
    if org is None or org.status != "active":
        raise APIError(403, "forbidden", "Organization is not active.")
    wait = check_rate_limit(key.id, rate_limit_per_minute)
    if wait is not None:
        raise APIError(429, "rate_limited", "API rate limit exceeded for this key.", {"retry_after": wait})
    now = utcnow()
    if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=1):
        key.last_used_at = now
        await session.commit()
    return Principal(org.id, key.id, frozenset(key.scopes), key.environment)


async def authenticate(request: Request, session: AsyncSession = Depends(get_session)) -> Principal:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise APIError(401, "unauthorized", "Missing or malformed Authorization header.")
    settings = request.app.state.runtime.settings
    principal = await principal_for_token(session, token, rate_limit_per_minute=settings.api_rate_limit_per_minute)
    request.state.principal = principal
    return principal


def require_scope(scope: str):
    async def dependency(principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.has(scope):
            raise APIError(403, "forbidden", f"API key lacks scope '{scope}'.")
        return principal

    return dependency
