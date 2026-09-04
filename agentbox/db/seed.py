from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.config import Settings
from agentbox.db.models import Domain, ProviderAccount, utcnow
from agentbox.domain.ids import new_id
from agentbox.security.crypto import encrypt_json


def default_smtp_config(settings: Settings) -> dict:
    return {
        "host": settings.outbound_smtp_host,
        "port": settings.outbound_smtp_port,
        "username": settings.outbound_smtp_username,
        "password": settings.outbound_smtp_password,
        "starttls": settings.outbound_smtp_starttls,
    }


async def ensure_seed_data(session: AsyncSession, settings: Settings) -> None:
    """Idempotently create the shared managed domain and the shared default SMTP relay account."""
    domain = await session.scalar(select(Domain).where(Domain.domain == settings.managed_domain))
    if domain is None:
        session.add(
            Domain(
                id=new_id("dom"), organization_id=None, domain=settings.managed_domain,
                type="agentbox_managed", status="active", inbound_status="active", outbound_status="active",
                verified_at=utcnow(),
            )
        )
    account = await session.scalar(
        select(ProviderAccount).where(
            ProviderAccount.organization_id.is_(None), ProviderAccount.provider == "smtp_relay"
        )
    )
    encrypted = encrypt_json(settings.app_secret_key, default_smtp_config(settings))
    if account is None:
        session.add(
            ProviderAccount(
                id=new_id("pa"), organization_id=None, provider="smtp_relay",
                name="default-smtp-relay", config_encrypted=encrypted, status="active",
            )
        )
    else:
        account.config_encrypted = encrypted
    await session.commit()
