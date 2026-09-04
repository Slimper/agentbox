import re

from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import generate_api_key
from agentbox.db.models import ApiKey, Organization
from agentbox.domain.ids import new_id

ALL_SCOPES = (
    "inboxes:read", "inboxes:write", "messages:read", "messages:send", "attachments:read",
    "attachments:write", "webhooks:read", "webhooks:write", "events:read", "domains:read", "domains:write",
    "policies:read", "policies:write", "suppressions:read", "suppressions:write", "providers:read", "providers:write",
    "approvals:write", "analytics:read", "usage:read", "keys:read", "keys:write", "admin",
)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


async def create_organization(session: AsyncSession, name: str, slug: str | None = None) -> Organization:
    org = Organization(id=new_id("org"), name=name, slug=slug or f"{slugify(name)}-{new_id('org')[-6:].lower()}")
    session.add(org)
    await session.flush()
    return org


async def create_api_key(
    session: AsyncSession, organization_id: str, *, name: str = "default",
    scopes: tuple[str, ...] = ("admin",), environment: str = "live",
) -> tuple[ApiKey, str]:
    for s in scopes:
        if s not in ALL_SCOPES:
            raise ValueError(f"unknown scope: {s}")
    plaintext, prefix, digest = generate_api_key(environment)
    key = ApiKey(id=new_id("key"), organization_id=organization_id, name=name, key_prefix=prefix,
                 key_hash=digest, scopes=list(scopes), environment=environment)
    session.add(key)
    await session.flush()
    return key, plaintext
