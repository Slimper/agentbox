from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.errors import APIError, not_found
from agentbox.config import Settings
from agentbox.db.models import Domain, Inbox, utcnow
from agentbox.domain.addresses import generate_username, validate_username
from agentbox.domain.ids import new_id
from agentbox.domain.ttl import parse_ttl
from agentbox.extensions import registry
from agentbox.jobs.queue import enqueue
from agentbox.services.events import emit


def inbox_to_dict(inbox: Inbox) -> dict:
    return {
        "id": inbox.id, "email": inbox.address, "username": inbox.username, "display_name": inbox.display_name,
        "status": inbox.status, "provider_mode": inbox.provider_mode, "metadata": inbox.metadata_,
        "expires_at": inbox.expires_at.isoformat() if inbox.expires_at else None,
        "created_at": inbox.created_at.isoformat(), "updated_at": inbox.updated_at.isoformat(),
    }


async def get_domain_for_org(session: AsyncSession, organization_id: str, domain_name: str) -> Domain | None:
    return await session.scalar(
        select(Domain).where(
            Domain.domain == domain_name.lower(), Domain.status == "active", Domain.deleted_at.is_(None),
            or_(Domain.organization_id.is_(None), Domain.organization_id == organization_id),
        )
    )


async def create_inbox(
    session: AsyncSession, *, organization_id: str, settings: Settings, username: str | None = None,
    domain: str | None = None, display_name: str | None = None, metadata: dict | None = None,
    ttl: str | None = None,
) -> Inbox:
    domain_row = await get_domain_for_org(session, organization_id, domain or settings.managed_domain)
    if domain_row is None:
        raise APIError(422, "validation_error", f"Domain '{domain or settings.managed_domain}' is not available.")
    for hook in registry().hooks(settings, "inbox.before_create"):
        await hook(session, organization_id, settings)
    expires_at = None
    if ttl:
        try:
            expires_at = utcnow() + parse_ttl(ttl)
        except ValueError as e:
            raise APIError(422, "validation_error", str(e)) from e
    generated = username is None
    if not generated:
        try:
            username = validate_username(username)
        except ValueError as e:
            raise APIError(422, "validation_error", str(e)) from e
    for _ in range(5):
        local = generate_username() if generated else username
        inbox = Inbox(
            id=new_id("ibx"), organization_id=organization_id, address=f"{local}@{domain_row.domain}",
            username=local, domain_id=domain_row.id, display_name=display_name, status="active",
            metadata_=metadata or {}, expires_at=expires_at,
        )
        try:
            async with session.begin_nested():
                session.add(inbox)
                await session.flush()
        except IntegrityError:
            if generated:
                continue
            raise APIError(409, "conflict", f"Address '{inbox.address}' is already in use.") from None
        break
    else:
        raise APIError(409, "conflict", "Could not allocate a unique address.")
    await emit(session, organization_id=organization_id, resource_type="inbox", resource_id=inbox.id,
               type="inbox.created", payload={"inbox_id": inbox.id, "inbox": inbox_to_dict(inbox)})
    if expires_at is not None:
        await enqueue(session, "inbox_expire", {"inbox_id": inbox.id}, run_at=expires_at)
    return inbox


async def get_inbox(session: AsyncSession, organization_id: str, inbox_id: str) -> Inbox:
    inbox = await session.scalar(
        select(Inbox).where(Inbox.id == inbox_id, Inbox.organization_id == organization_id,
                            Inbox.deleted_at.is_(None))
    )
    if inbox is None:
        raise not_found("inbox", inbox_id)
    return inbox


async def get_active_inbox_for_send(session: AsyncSession, organization_id: str, inbox_id: str) -> Inbox:
    inbox = await get_inbox(session, organization_id, inbox_id)
    if inbox.status != "active":
        raise APIError(409, "inbox_disabled", f"Inbox is {inbox.status} and cannot send.")
    return inbox
