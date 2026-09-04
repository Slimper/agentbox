import secrets

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.config import Settings
from agentbox.db.models import ProviderAccount, RoutingRule
from agentbox.providers.base import OutboundProvider, TemporaryError
from agentbox.providers.sendgrid import SendGridProvider
from agentbox.providers.smtp_relay import SMTPRelayProvider
from agentbox.providers.unisender import UnisenderGoProvider
from agentbox.security.crypto import decrypt_json

PROVIDERS = ("smtp_relay", "sendgrid", "unisender_go")


def rule_matches(rule: RoutingRule, *, inbox_id: str | None, recipient_domain: str | None) -> bool:
    m = rule.match or {}
    if m.get("inbox_id") and m["inbox_id"] != inbox_id:
        return False
    suffix = (m.get("recipient_domain_suffix") or "").lower().lstrip(".")
    if suffix:
        d = (recipient_domain or "").lower()
        if not (d == suffix or d.endswith("." + suffix)):
            return False
    return True


async def select_provider_account(
    session: AsyncSession, organization_id: str, *, inbox_id: str | None = None, recipient_domain: str | None = None
) -> ProviderAccount:
    rules = await session.scalars(
        select(RoutingRule).where(RoutingRule.organization_id == organization_id)
        .order_by(RoutingRule.priority, RoutingRule.id)
    )
    for rule in rules:
        if rule_matches(rule, inbox_id=inbox_id, recipient_domain=recipient_domain):
            account = await session.get(ProviderAccount, rule.provider_account_id)
            if account is not None and account.status == "active":
                return account
    own = await session.scalar(
        select(ProviderAccount).where(ProviderAccount.organization_id == organization_id,
                                      ProviderAccount.status == "active").order_by(ProviderAccount.created_at)
    )
    if own is not None:
        return own
    shared = await session.scalar(
        select(ProviderAccount).where(ProviderAccount.organization_id.is_(None), ProviderAccount.status == "active")
        .order_by(ProviderAccount.created_at)
    )
    if shared is None:
        raise TemporaryError("no active provider account configured")
    return shared


def build_provider(
    account: ProviderAccount, settings: Settings, http: httpx.AsyncClient | None = None
) -> OutboundProvider:
    cfg = decrypt_json(settings.app_secret_key, account.config_encrypted)
    if account.provider == "smtp_relay":
        return SMTPRelayProvider(host=cfg["host"], port=cfg["port"], username=cfg.get("username"),
                                 password=cfg.get("password"), starttls=cfg.get("starttls", False))
    http = http or httpx.AsyncClient(timeout=30.0)
    if account.provider == "sendgrid":
        return SendGridProvider(api_key=cfg["api_key"], http=http,
                                base_url=cfg.get("base_url") or settings.sendgrid_api_base)
    if account.provider == "unisender_go":
        return UnisenderGoProvider(api_key=cfg["api_key"], http=http,
                                   base_url=cfg.get("base_url") or settings.unisender_api_base)
    raise TemporaryError(f"unsupported provider '{account.provider}'")


def parse_provider_events(provider: str, payload):
    if provider == "sendgrid":
        return SendGridProvider.parse_events(payload)
    if provider == "unisender_go":
        return UnisenderGoProvider.parse_events(payload)
    return []


def new_webhook_token() -> str:
    return secrets.token_urlsafe(24)


def return_path_for(message_id: str, domain: str) -> str:
    return f"bounce+{message_id.split('_', 1)[1]}@{domain}"
