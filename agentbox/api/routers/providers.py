from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import APIError, not_found
from agentbox.api.schemas import ProviderAccountCreate, RoutingRuleCreate
from agentbox.db.models import ProviderAccount, RoutingRule
from agentbox.domain.ids import new_id
from agentbox.providers.router import build_provider, new_webhook_token
from agentbox.runtime import Runtime
from agentbox.security.crypto import decrypt_json, encrypt_json
from agentbox.services.events import emit

router = APIRouter(prefix="/v1", tags=["providers"])

REQUIRED = {"smtp_relay": ("host", "port"), "sendgrid": ("api_key",), "unisender_go": ("api_key",)}
SAFE_KEYS = {"host", "port", "username", "starttls", "base_url"}


def account_to_dict(a: ProviderAccount, settings, base_url: str) -> dict:
    cfg = decrypt_json(settings.app_secret_key, a.config_encrypted)
    return {
        "id": a.id, "provider": a.provider, "name": a.name, "status": a.status, "shared": a.organization_id is None,
        "config": {k: v for k, v in cfg.items() if k in SAFE_KEYS}, "config_keys": sorted(cfg),
        "events_url": f"{base_url}/v1/providers/{a.provider}/events/{a.webhook_token}" if a.webhook_token else None,
        "created_at": a.created_at.isoformat(),
    }


def rule_to_dict(r: RoutingRule) -> dict:
    return {"id": r.id, "priority": r.priority, "match": r.match, "provider_account_id": r.provider_account_id,
            "description": r.description, "created_at": r.created_at.isoformat()}


async def _account(session: AsyncSession, org_id: str, account_id: str) -> ProviderAccount:
    a = await session.scalar(select(ProviderAccount).where(ProviderAccount.id == account_id,
                                                           ProviderAccount.organization_id == org_id))
    if a is None:
        raise not_found("provider_account", account_id)
    return a


@router.get("/provider-accounts")
async def list_accounts(principal: Principal = Depends(require_scope("providers:read")),
                        session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    rows = await session.scalars(
        select(ProviderAccount).where(ProviderAccount.organization_id == principal.organization_id,
                                      ProviderAccount.status != "deleted").order_by(ProviderAccount.created_at)
    )
    return {"data": [account_to_dict(a, runtime.settings, runtime.settings.api_base_url) for a in rows],
            "next_cursor": None}


@router.post("/provider-accounts", status_code=201)
async def create_account(body: ProviderAccountCreate, principal: Principal = Depends(require_scope("providers:write")),
                         session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    missing = [k for k in REQUIRED[body.provider] if k not in body.config]
    if missing:
        raise APIError(422, "validation_error", f"config missing keys: {missing}")
    a = ProviderAccount(id=new_id("pa"), organization_id=principal.organization_id, provider=body.provider,
                        name=body.name, config_encrypted=encrypt_json(runtime.settings.app_secret_key, body.config),
                        status="active", webhook_token=new_webhook_token())
    session.add(a)
    await session.flush()
    await emit(session, organization_id=principal.organization_id, resource_type="provider_account", resource_id=a.id,
               type="provider.changed", payload={"action": "created", "provider": a.provider, "name": a.name,
                                                  "actor": principal.api_key_id})
    await session.commit()
    return account_to_dict(a, runtime.settings, runtime.settings.api_base_url)


@router.post("/provider-accounts/{account_id}/test")
async def test_account(account_id: str, principal: Principal = Depends(require_scope("providers:write")),
                       session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    a = await _account(session, principal.organization_id, account_id)
    provider = build_provider(a, runtime.settings, runtime.http)
    return {"id": a.id, "healthy": await provider.health()}


@router.delete("/provider-accounts/{account_id}", status_code=204, response_class=Response)
async def delete_account(account_id: str, principal: Principal = Depends(require_scope("providers:write")),
                         session: AsyncSession = Depends(get_session)):
    a = await _account(session, principal.organization_id, account_id)
    rules = (await session.scalars(select(RoutingRule).where(RoutingRule.provider_account_id == a.id))).all()
    for r in rules:
        await session.delete(r)
    a.status = "deleted"
    a.webhook_token = None
    await emit(session, organization_id=principal.organization_id, resource_type="provider_account", resource_id=a.id,
               type="provider.changed", payload={"action": "deleted", "actor": principal.api_key_id})
    await session.commit()
    return Response(status_code=204)


@router.get("/routing-rules")
async def list_rules(principal: Principal = Depends(require_scope("providers:read")),
                     session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(RoutingRule).where(RoutingRule.organization_id == principal.organization_id)
                                 .order_by(RoutingRule.priority, RoutingRule.id))
    return {"data": [rule_to_dict(r) for r in rows], "next_cursor": None}


@router.post("/routing-rules", status_code=201)
async def create_rule(body: RoutingRuleCreate, principal: Principal = Depends(require_scope("providers:write")),
                      session: AsyncSession = Depends(get_session)):
    a = await _account(session, principal.organization_id, body.provider_account_id)
    if a.status != "active":
        raise APIError(409, "conflict", "Provider account is not active.")
    r = RoutingRule(id=new_id("rr"), organization_id=principal.organization_id, priority=body.priority,
                    match=body.match, provider_account_id=a.id, description=body.description)
    session.add(r)
    await session.commit()
    return rule_to_dict(r)


@router.delete("/routing-rules/{rule_id}", status_code=204, response_class=Response)
async def delete_rule(rule_id: str, principal: Principal = Depends(require_scope("providers:write")),
                      session: AsyncSession = Depends(get_session)):
    r = await session.scalar(select(RoutingRule).where(RoutingRule.id == rule_id,
                                                       RoutingRule.organization_id == principal.organization_id))
    if r is None:
        raise not_found("routing_rule", rule_id)
    await session.delete(r)
    await session.commit()
    return Response(status_code=204)
