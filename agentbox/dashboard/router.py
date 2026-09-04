"""AgentBox Console — operator dashboard rendered server-side on the Donkit design system.

Implements the `AgentBox Console` design (sidebar shell, overview with activation / operations / live feed,
inboxes + thread viewer, domains, webhooks, API keys, usage, policies, audit, quickstart, API console) on top of
the same services the public API uses. Login with an API key; the key is stored encrypted in an HttpOnly cookie."""

from __future__ import annotations

import csv
import io
import json
import secrets
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import nh3
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, principal_for_token
from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import APIError
from agentbox.dashboard.icons import ICONS
from agentbox.db.models import (
    ApiKey,
    Domain,
    Event,
    Inbox,
    Message,
    Organization,
    ProviderAccount,
    RoutingRule,
    Suppression,
    Thread,
    UsageDaily,
    Webhook,
    WebhookDelivery,
    utcnow,
)
from agentbox.domain.ids import new_id
from agentbox.domains.verify import domain_to_dict
from agentbox.extensions import registry
from agentbox.governance.policies import (
    DEFAULT_POLICY,
    get_effective_policy,
    get_policy_row,
    set_policy,
    validate_policy_config,
)
from agentbox.governance.suppressions import add_suppression
from agentbox.jobs.queue import enqueue
from agentbox.runtime import Runtime
from agentbox.security.crypto import decrypt_str, encrypt_str
from agentbox.services.attachments import PRESIGN_GET_SECONDS, attachments_for_messages, get_attachment
from agentbox.services.events import emit
from agentbox.services.inboxes import create_inbox, get_inbox, inbox_to_dict
from agentbox.services.messages import (
    OutboundDraft,
    approve_message,
    build_reply_draft,
    create_outbound_message,
    get_message,
    reject_message,
)
from agentbox.services.organizations import ALL_SCOPES, create_api_key
from agentbox.usage.rollup import compute_usage
from agentbox.webhooks.delivery import generate_secret

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
_TEMPLATE_DIRS = [str(Path(__file__).parent / "templates")]
templates = Jinja2Templates(env=Environment(loader=ChoiceLoader([FileSystemLoader(d) for d in _TEMPLATE_DIRS]),
                                            autoescape=select_autoescape(["html", "xml"])))
COOKIE = "ab_dash"
THEME_COOKIE = "ab_theme"

NAV = [("overview", "Overview", "dashboard", "/dashboard"), ("inboxes", "Inboxes", "inbox", "/dashboard/inboxes"),
       ("domains", "Domains", "globe", "/dashboard/domains"), ("webhooks", "Webhooks", "webhook", "/dashboard/webhooks"),
       ("keys", "API Keys", "key", "/dashboard/api-keys"), ("usage", "Usage & Billing", "chart", "/dashboard/usage"),
       ("policies", "Policies", "shield", "/dashboard/policies"), ("audit", "Audit Log", "scroll", "/dashboard/audit")]
NAV2 = [("quickstart", "Quickstart", "zap", "/dashboard/quickstart"), ("console", "API Console", "terminal", "/dashboard/console")]

STATUS_KIND = {
    "active": "success", "suspended": "error", "expired": "default", "deleted": "default", "provisioning": "info",
    "verification_pending": "warning", "degraded": "warning", "failed": "error", "managed": "default",
    "provider_accepted": "success", "delivered": "success", "queued": "info", "stored": "default", "received": "default",
    "deferred": "warning", "bounced": "error", "rejected": "error", "complained": "error", "pending_approval": "warning",
    "succeeded": "success", "exhausted": "error", "pending": "info", "disabled": "default",
    "ok": "success", "partial": "warning", "missing": "error", "wrong": "error", "skipped": "default", "unknown": "default",
}
EVENT_DOT = {
    "received": "var(--data-blue)", "delivered": "var(--color-status-success)", "provider_accepted": "var(--color-status-success)",
    "failed": "var(--color-status-error)", "bounced": "var(--color-status-error)", "rejected": "var(--color-status-error)",
    "blocked": "var(--color-status-warning)", "deferred": "var(--color-status-warning)", "required": "var(--color-status-warning)",
    "degraded": "var(--color-status-warning)", "verified": "var(--color-status-success)", "queued": "var(--color-fg-disabled)",
}


class LoginRequired(Exception):
    pass


# ---------------------------------------------------------------- helpers

def badge(status: str | None, size: str = "s") -> str:
    return f"ds-badge ds-badge--{STATUS_KIND.get(status or '', 'default')} ds-badge--{size}"


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    delta = utcnow() - dt
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    if s < 7 * 86400:
        return f"{s // 86400} d ago"
    return dt.strftime("%d %b %Y")


def hhmm(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    now = utcnow()
    if dt.date() == now.date():
        return dt.strftime("%H:%M:%S")
    if dt.date() == (now - timedelta(days=1)).date():
        return "Yesterday " + dt.strftime("%H:%M")
    return dt.strftime("%d %b %H:%M")


def event_text(e: Event) -> str:
    p = e.payload or {}
    m = p.get("message") or {}
    if e.type == "message.received":
        frm = (m.get("from") or {}).get("email", "?")
        atts = ", ".join(a.get("filename", "") for a in m.get("attachments", []))
        return f"{frm} → inbox {p.get('inbox_id', '')}" + (f" · {atts}" if atts else "")
    if e.type.startswith("message."):
        bits = [p.get("provider") or "", p.get("reason") or p.get("error") or p.get("reason_code") or ""]
        return f"{p.get('message_id', '')} · " + " · ".join(b for b in bits if b)
    if e.type == "policy.blocked":
        return f"{', '.join(p.get('recipients', []))} · {p.get('reason', '')}"
    if e.type.startswith("inbox."):
        return (p.get("inbox") or {}).get("email") or p.get("inbox_id", "")
    if e.type.startswith("domain."):
        return (p.get("domain") or {}).get("domain") or p.get("domain_id", "")
    if e.type.startswith("webhook."):
        return f"{p.get('webhook_id', '')} · {p.get('error', '')}"
    if e.type.startswith("approval."):
        return f"{p.get('message_id', '')} · {', '.join(p.get('reasons', []))}" if p.get("reasons") else p.get("message_id", "")
    if e.type.startswith("api_key."):
        return p.get("name", "")
    if e.type.startswith("suppression."):
        return (p.get("suppression") or {}).get("email") or p.get("email", "")
    return json.dumps({k: v for k, v in p.items() if k not in ("inbox", "message")}, ensure_ascii=False)[:120]


def event_view(e: Event) -> dict:
    kind = e.type.split(".")[-1]
    return {"id": e.id, "type": e.type, "text": event_text(e), "time": hhmm(e.created_at),
            "dot": EVENT_DOT.get(kind, "var(--color-fg-secondary)"), "actor": (e.payload or {}).get("actor", ""),
            "resource": f"{e.resource_type}/{e.resource_id}", "payload": e.payload, "created_at": e.created_at}


def _clean_html(html: str | None) -> str:
    if not html:
        return ""
    return nh3.clean(html, link_rel="noopener noreferrer nofollow", url_schemes={"http", "https", "mailto", "cid"})


def _scope(principal: Principal, scope: str) -> None:
    if not principal.has(scope):
        raise APIError(403, "forbidden", f"API key lacks scope '{scope}'.")


async def dash_principal(request: Request, session: AsyncSession = Depends(get_session),
                         runtime: Runtime = Depends(get_runtime)) -> Principal:
    for hook in registry().hooks(runtime.settings, "dashboard.principal"):
        principal = await hook(request, session, runtime)
        if principal is not None:
            return principal
    token = request.cookies.get(COOKIE)
    if not token:
        raise LoginRequired()
    try:
        api_key = decrypt_str(runtime.settings.app_secret_key, token)
        return await principal_for_token(session, api_key)
    except Exception as e:  # noqa: BLE001
        raise LoginRequired() from e


async def _shell(request: Request, session: AsyncSession, principal: Principal | None, page: str) -> dict:
    ctx: dict = {"page": page, "principal": principal, "now": utcnow(), "icons": ICONS, "badge": badge, "ago": ago,
                 "hhmm": hhmm, "theme": request.cookies.get(THEME_COOKIE, "dark"), "nav": NAV, "nav2": NAV2,
                 "toast": request.query_params.get("toast"), "path": request.url.path}
    settings = request.app.state.runtime.settings
    ctx["edition"] = settings.edition
    ctx["paths"] = registry().paths(settings)
    ctx["plan_note"] = ""
    if principal is not None:
        org = await session.get(Organization, principal.organization_id)
        user = getattr(request.state, "user", None)
        key = None if user is not None else await session.get(ApiKey, principal.api_key_id)
        active = await session.scalar(select(func.count()).select_from(Inbox).where(
            Inbox.organization_id == principal.organization_id, Inbox.deleted_at.is_(None), Inbox.status == "active"))
        plan = org.plan if org else "free"
        plan_label = {"free": "Free", "payg": "Pay-as-you-go", "enterprise": "Enterprise"}.get(plan, plan.title())
        ctx.update({"org": org, "org_name": org.name if org else "", "org_plan": plan_label, "plan": plan,
                    "env": principal.environment, "key_name": key.name if key else (user.name or user.email if user else ""),
                    "active_inboxes": active or 0, "user": user,
                    "membership": getattr(request.state, "membership", None),
                    "organizations": getattr(request.state, "organizations", []),
                    "inbox_cap": None,
                    "initials": "".join(w[0] for w in ((user.name or user.email) if user else (org.name if org else "AB")).split()[:2]).upper() or "AB"})
    for hook in registry().hooks(settings, "dashboard.shell"):
        ctx.update(await hook(request, session, principal, ctx) or {})
    return ctx


def _render(request: Request, name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _redirect(url: str, toast: str | None = None) -> RedirectResponse:
    if toast:
        url += ("&" if "?" in url else "?") + "toast=" + quote(toast)
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- auth / theme

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None, key: str | None = None,
                     session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    login_path = registry().paths(runtime.settings)["login"]
    if login_path != "/dashboard/login" and not key:
        return RedirectResponse(login_path, status_code=303)
    return _render(request, "login.html", {**await _shell(request, session, None, "login"), "error": error})


@router.post("/login")
async def login(request: Request, api_key: str = Form(...), session: AsyncSession = Depends(get_session),
                runtime: Runtime = Depends(get_runtime)):
    try:
        await principal_for_token(session, api_key)
    except APIError:
        return _render(request, "login.html", {**await _shell(request, session, None, "login"), "error": "Invalid API key."})
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(COOKIE, encrypt_str(runtime.settings.app_secret_key, api_key.strip()), httponly=True,
                    samesite="lax", max_age=12 * 3600)
    return resp


@router.post("/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    for hook in registry().hooks(runtime.settings, "dashboard.logout"):
        await hook(request, session)
    resp = RedirectResponse(registry().paths(runtime.settings)["login"], status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.post("/theme")
async def toggle_theme(request: Request, next: str = Form("/dashboard")):
    current = request.cookies.get(THEME_COOKIE, "dark")
    resp = RedirectResponse(next if next.startswith("/dashboard") else "/dashboard", status_code=303)
    resp.set_cookie(THEME_COOKIE, "light" if current == "dark" else "dark", samesite="lax", max_age=365 * 86400)
    return resp


# ---------------------------------------------------------------- overview

async def _funnel(session: AsyncSession, org_id: str) -> list[dict]:
    def one(stmt):
        return session.scalar(stmt.limit(1))

    first_inbox = await one(select(Inbox.address).where(Inbox.organization_id == org_id).order_by(Inbox.created_at))
    first_sent = await one(select(Message.id).where(Message.organization_id == org_id, Message.direction == "outbound")
                           .order_by(Message.created_at))
    first_delivered = await one(select(Message).where(
        Message.organization_id == org_id, Message.direction == "outbound",
        Message.status.in_(["provider_accepted", "delivered"])).order_by(Message.created_at))
    first_inbound = await one(select(Message.id).where(Message.organization_id == org_id, Message.direction == "inbound")
                              .order_by(Message.created_at))
    first_reply = await one(select(Message.id).where(Message.organization_id == org_id, Message.direction == "outbound",
                                                     Message.in_reply_to.is_not(None)).order_by(Message.created_at))
    hook = await one(select(Webhook.url).where(Webhook.organization_id == org_id, Webhook.deleted_at.is_(None)))
    key_name = await one(select(ApiKey.name).where(ApiKey.organization_id == org_id).order_by(ApiKey.created_at))
    steps = [
        ("API key created", True, key_name or "", None, None),
        ("First inbox created", bool(first_inbox), first_inbox or "", "Create inbox", "/dashboard/inboxes?create=managed"),
        ("First email sent", bool(first_sent), first_sent or "", "Send test", None),
        ("First email accepted by provider", bool(first_delivered),
         f"{first_delivered.provider} · {first_delivered.status}" if first_delivered else "", None, None),
        ("First inbound reply received", bool(first_inbound), first_inbound or "waiting for MX traffic", "How to test",
         "/dashboard/quickstart"),
        ("First reply sent", bool(first_reply), first_reply or "", None, None),
        ("Custom webhook registered", bool(hook), urlparse(hook).netloc if hook else "", "Add endpoint",
         "/dashboard/webhooks?create=1"),
    ]
    out, seen_now = [], False
    for label, done, meta, cta, href in steps:
        state = "done" if done else ("now" if not seen_now else "todo")
        if state == "now":
            seen_now = True
        out.append({"label": label, "state": state, "meta": meta, "cta": cta if state != "done" else None, "href": href})
    return out


async def _stats(session: AsyncSession, org_id: str) -> dict:
    day = utcnow() - timedelta(days=1)
    month = utcnow() - timedelta(days=30)
    q = select(func.count()).select_from(Message).where(Message.organization_id == org_id)
    sent = await session.scalar(q.where(Message.direction == "outbound", Message.created_at >= day))
    received = await session.scalar(q.where(Message.direction == "inbound", Message.created_at >= day))
    out_total = await session.scalar(q.where(Message.direction == "outbound", Message.created_at >= month,
                                             Message.status.not_in(["queued", "pending_approval"])))
    out_ok = await session.scalar(q.where(Message.direction == "outbound", Message.created_at >= month,
                                          Message.status.in_(["provider_accepted", "delivered", "deferred"])))
    bounced = await session.scalar(q.where(Message.direction == "outbound", Message.created_at >= month,
                                           Message.status == "bounced"))
    complained = await session.scalar(q.where(Message.direction == "outbound", Message.created_at >= month,
                                              Message.status == "complained"))
    blocked = await session.scalar(select(func.count()).select_from(Event).where(
        Event.organization_id == org_id, Event.type == "policy.blocked", Event.created_at >= day))
    wq = select(func.count()).select_from(WebhookDelivery).where(WebhookDelivery.organization_id == org_id,
                                                                 WebhookDelivery.created_at >= day)
    w_total = await session.scalar(wq.where(WebhookDelivery.status != "pending"))
    w_ok = await session.scalar(wq.where(WebhookDelivery.status == "succeeded"))
    w_retry = await session.scalar(wq.where(WebhookDelivery.status == "pending"))
    active = await session.scalar(select(func.count()).select_from(Inbox).where(
        Inbox.organization_id == org_id, Inbox.deleted_at.is_(None), Inbox.status == "active"))
    new_week = await session.scalar(select(func.count()).select_from(Inbox).where(
        Inbox.organization_id == org_id, Inbox.created_at >= utcnow() - timedelta(days=7)))
    rate = f"{100 * out_ok / out_total:.1f}%" if out_total else "—"
    wrate = f"{100 * w_ok / w_total:.1f}%" if w_total else "—"
    return {"active": active or 0, "sent": sent or 0, "received": received or 0, "rate": rate, "wrate": wrate,
            "blocked": blocked or 0, "bounced": bounced or 0, "complained": complained or 0, "new_week": new_week or 0,
            "w_retry": w_retry or 0}


async def _chart(session: AsyncSession, org_id: str) -> list[dict]:
    start = (utcnow() - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_col = func.date(Message.created_at).label("d")
    rows = await session.execute(
        select(day_col, Message.direction, func.count())
        .where(Message.organization_id == org_id, Message.created_at >= start)
        .group_by(day_col, Message.direction))
    counts: dict[str, Counter] = defaultdict(Counter)
    for day, direction, n in rows:
        counts[day.isoformat()][direction] = n
    days = [(start + timedelta(days=i)).date() for i in range(14)]
    peak = max([1] + [max(c["outbound"], c["inbound"]) for c in counts.values()])
    return [{"day": d.strftime("%-d"), "sent": counts[d.isoformat()]["outbound"], "received": counts[d.isoformat()]["inbound"],
             "s": round(100 * counts[d.isoformat()]["outbound"] / peak), "r": round(100 * counts[d.isoformat()]["inbound"] / peak)}
            for d in days]


async def _providers(session: AsyncSession, org_id: str) -> list[dict]:
    month = utcnow() - timedelta(days=30)
    provider_col = func.coalesce(Message.provider, "unassigned").label("provider")
    rows = await session.execute(
        select(provider_col, Message.status, func.count())
        .where(Message.organization_id == org_id, Message.direction == "outbound", Message.created_at >= month)
        .group_by(provider_col, Message.status))
    by: dict[str, Counter] = defaultdict(Counter)
    for provider, status, n in rows:
        by[provider][status] = n
    routes = {r.provider_account_id: r.match for r in await session.scalars(
        select(RoutingRule).where(RoutingRule.organization_id == org_id))}
    accounts = {a.id: a for a in await session.scalars(select(ProviderAccount).where(
        or_(ProviderAccount.organization_id == org_id, ProviderAccount.organization_id.is_(None))))}
    out = []
    for provider, c in by.items():
        total = sum(v for k, v in c.items() if k not in ("queued", "pending_approval")) or 0
        ok = c["provider_accepted"] + c["delivered"]
        pct = (100 * ok / total) if total else 0
        bounce = (100 * c["bounced"] / total) if total else 0
        healthy = bounce < 5
        route = next((m.get("recipient_domain_suffix") and f"*.{m['recipient_domain_suffix']} recipients"
                      for aid, m in routes.items() if accounts.get(aid) and accounts[aid].provider == provider), None)
        out.append({"name": provider, "route": route or "default route", "status": "healthy" if healthy else "degraded",
                    "kind": "success" if healthy else "warning", "delivered": f"{pct:.1f}%",
                    "deferred": f"{(100 * c['deferred'] / total) if total else 0:.1f}%", "bounced": f"{bounce:.1f}%",
                    "bar": round(pct), "total": total})
    return sorted(out, key=lambda p: -p["total"])


async def _attention(session: AsyncSession, org_id: str) -> list[dict]:
    items = []
    day = utcnow() - timedelta(days=1)
    failing = await session.execute(
        select(Webhook, func.count(WebhookDelivery.id)).join(WebhookDelivery, WebhookDelivery.webhook_id == Webhook.id)
        .where(Webhook.organization_id == org_id, Webhook.deleted_at.is_(None), WebhookDelivery.created_at >= day,
               WebhookDelivery.status.in_(["failed", "exhausted"])).group_by(Webhook.id))
    for hook, n in failing:
        items.append({"title": f"{urlparse(hook.url).netloc} webhook failing", "sub": f"{n} failed deliveries · 24h",
                      "icon": "webhook", "color": "var(--color-status-error)", "href": f"/dashboard/webhooks?hook={hook.id}"})
    for d in await session.scalars(select(Domain).where(Domain.organization_id == org_id, Domain.deleted_at.is_(None),
                                                        Domain.status != "active")):
        miss = ", ".join(f"{k} {v}" for k, v in (d.check_results or {}).items() if v in ("missing", "wrong")) or "not checked yet"
        items.append({"title": f"{d.domain} {d.status.replace('_', ' ')}", "sub": miss, "icon": "globe",
                      "color": "var(--color-status-warning)", "href": f"/dashboard/domains?domain={d.id}"})
    for i in await session.scalars(select(Inbox).where(Inbox.organization_id == org_id, Inbox.status == "suspended",
                                                       Inbox.deleted_at.is_(None)).limit(3)):
        items.append({"title": f"{i.username} suspended", "sub": i.address, "icon": "shield",
                      "color": "var(--color-status-warning)", "href": f"/dashboard/inboxes/{i.id}"})
    pending = await session.scalar(select(func.count()).select_from(Message).where(
        Message.organization_id == org_id, Message.status == "pending_approval"))
    if pending:
        items.append({"title": f"{pending} message(s) waiting for approval", "sub": "approval gate triggered",
                      "icon": "alert", "color": "var(--color-status-warning)", "href": "/dashboard/approvals"})
    return items


async def _top_inboxes(session: AsyncSession, org_id: str) -> list[dict]:
    day = utcnow() - timedelta(days=1)
    rows = await session.execute(
        select(Inbox, func.count(Message.id).filter(Message.direction == "outbound"),
               func.count(Message.id).filter(Message.direction == "inbound"))
        .outerjoin(Message, (Message.inbox_id == Inbox.id) & (Message.created_at >= day))
        .where(Inbox.organization_id == org_id, Inbox.deleted_at.is_(None)).group_by(Inbox.id)
        .order_by(desc(func.count(Message.id))).limit(5))
    return [{"id": i.id, "address": i.address, "sent": s, "received": r} for i, s, r in rows]


async def _webhook_health(session: AsyncSession, org_id: str) -> list[dict]:
    day = utcnow() - timedelta(days=1)
    rows = await session.execute(
        select(Webhook, func.count(WebhookDelivery.id).filter(WebhookDelivery.status == "succeeded"),
               func.count(WebhookDelivery.id).filter(WebhookDelivery.status != "pending"))
        .outerjoin(WebhookDelivery, (WebhookDelivery.webhook_id == Webhook.id) & (WebhookDelivery.created_at >= day))
        .where(Webhook.organization_id == org_id, Webhook.deleted_at.is_(None)).group_by(Webhook.id))
    return [{"id": h.id, "host": urlparse(h.url).netloc, "url": h.url, "ok": total == 0 or ok / total >= 0.95,
             "rate": f"{100 * ok / total:.1f}%" if total else "no traffic", "total": total}
            for h, ok, total in rows]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, view: str = Query("activation", pattern="^(activation|operations|feed)$"),
                   feed: str = Query("all", pattern="^(all|inbound|outbound|policy)$"),
                   principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session)):
    org = principal.organization_id
    ctx = await _shell(request, session, principal, "overview")
    stats = await _stats(session, org)
    ev_stmt = select(Event).where(Event.organization_id == org)
    if feed == "inbound":
        ev_stmt = ev_stmt.where(Event.type == "message.received")
    elif feed == "outbound":
        ev_stmt = ev_stmt.where(Event.type.in_(["message.queued", "message.provider_accepted", "message.delivered",
                                                "message.deferred", "message.bounced", "message.rejected", "message.failed"]))
    elif feed == "policy":
        ev_stmt = ev_stmt.where(Event.type.in_(["policy.blocked", "policy.changed", "approval.required"]))
    events = [event_view(e) for e in await session.scalars(ev_stmt.order_by(desc(Event.id)).limit(40))]
    today = await session.scalar(select(func.count()).select_from(Event).where(
        Event.organization_id == org, Event.created_at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0)))
    ctx.update({"view": view, "feed": feed, "stats": stats, "events": events, "events_today": today or 0,
                "hello_code": HELLO_CODE})
    if view == "activation":
        funnel = await _funnel(session, org)
        done = sum(1 for f in funnel if f["state"] == "done")
        ctx.update({"funnel": funnel, "funnel_done": done, "funnel_pct": round(100 * done / len(funnel))})
    elif view == "operations":
        ctx.update({"chart": await _chart(session, org), "providers": await _providers(session, org),
                    "attention": await _attention(session, org), "top_inboxes": await _top_inboxes(session, org)})
    else:
        ctx.update({"hooks": await _webhook_health(session, org)})
    return _render(request, "overview.html", ctx)


HELLO_CODE = '''from agentbox_sdk import AgentBox

mail = AgentBox("ab_live_...", base_url="{base}")
inbox = mail.inboxes.create()

mail.messages.send(
    inbox["id"],
    to=["me@example.com"],
    subject="Hello from my agent",
    text="Sent by an AI agent.",
)
reply = mail.messages.wait_for(inbox["id"], timeout=300)'''


# ---------------------------------------------------------------- inboxes

@router.get("/inboxes", response_class=HTMLResponse)
async def inboxes(request: Request, filter: str = Query("all", pattern="^(all|managed|ephemeral|suspended)$"),
                  q: str = "", create: str | None = None, principal: Principal = Depends(dash_principal),
                  session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    org = principal.organization_id
    stmt = select(Inbox).where(Inbox.organization_id == org, Inbox.deleted_at.is_(None))
    if filter == "managed":
        stmt = stmt.where(Inbox.expires_at.is_(None))
    elif filter == "ephemeral":
        stmt = stmt.where(Inbox.expires_at.is_not(None))
    elif filter == "suspended":
        stmt = stmt.where(Inbox.status != "active")
    if q:
        stmt = stmt.where(or_(Inbox.address.ilike(f"%{q}%"), Inbox.metadata_.cast(str).ilike(f"%{q}%")))
    rows = list(await session.scalars(stmt.order_by(desc(Inbox.id)).limit(200)))
    day = utcnow() - timedelta(days=1)
    stats: dict[str, Counter] = defaultdict(Counter)
    for inbox_id, direction, n in await session.execute(
        select(Message.inbox_id, Message.direction, func.count()).where(Message.organization_id == org,
                                                                        Message.created_at >= day)
        .group_by(Message.inbox_id, Message.direction)):
        stats[inbox_id][direction] = n
    last = dict((await session.execute(select(Message.inbox_id, func.max(Message.created_at))
                                       .where(Message.organization_id == org).group_by(Message.inbox_id))).all())
    counts = Counter(i.status for i in await session.scalars(
        select(Inbox).where(Inbox.organization_id == org, Inbox.deleted_at.is_(None))))
    domains = list(await session.scalars(select(Domain).where(
        or_(Domain.organization_id == org, Domain.organization_id.is_(None)), Domain.status == "active",
        Domain.deleted_at.is_(None)).order_by(Domain.organization_id.is_(None), Domain.domain)))
    policy = await get_effective_policy(session, org, None)
    ctx = await _shell(request, session, principal, "inboxes")
    ctx.update({"inboxes": [{"i": i, "sent": stats[i.id]["outbound"], "received": stats[i.id]["inbound"],
                             "last": ago(last.get(i.id)),
                             "mode": "ephemeral · " + _ttl_left(i.expires_at) if i.expires_at else i.provider_mode,
                             "meta": ", ".join(f"{k}={v}" for k, v in (i.metadata_ or {}).items()) or "—"} for i in rows],
                "filter": filter, "q": q, "create": create, "counts": counts, "domains": domains,
                "managed_domain": runtime.settings.managed_domain, "policy": policy})
    return _render(request, "inboxes.html", ctx)


def _ttl_left(expires_at: datetime | None) -> str:
    if not expires_at:
        return ""
    s = int((expires_at - utcnow()).total_seconds())
    if s <= 0:
        return "expired"
    return f"{s // 3600}h left" if s >= 3600 else f"{max(1, s // 60)}m left"


@router.post("/inboxes")
async def create_inbox_form(username: str = Form(""), domain: str = Form(""), display_name: str = Form(""),
                            metadata: str = Form(""), ttl: str = Form(""), principal: Principal = Depends(dash_principal),
                            session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    _scope(principal, "inboxes:write")
    meta = {}
    if metadata.strip():
        try:
            meta = json.loads(metadata)
            if not isinstance(meta, dict):
                raise ValueError("metadata must be a JSON object")
        except ValueError as e:
            raise APIError(422, "validation_error", f"Invalid metadata JSON: {e}") from e
    inbox = await create_inbox(session, organization_id=principal.organization_id, settings=runtime.settings,
                               username=username.strip() or None, domain=domain.strip() or None,
                               display_name=display_name.strip() or None, metadata=meta, ttl=ttl.strip() or None)
    await session.commit()
    return _redirect(f"/dashboard/inboxes/{inbox.id}", f"Inbox {inbox.address} created · {inbox.id}")


@router.get("/inboxes/{inbox_id}", response_class=HTMLResponse)
async def inbox_detail(request: Request, inbox_id: str, tab: str = Query("threads", pattern="^(threads|policies|events|metadata)$"),
                       thread: str | None = None, send: str | None = None,
                       principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session)):
    org = principal.organization_id
    inbox = await get_inbox(session, org, inbox_id)
    ctx = await _shell(request, session, principal, "inboxes")
    threads = list(await session.scalars(select(Thread).where(Thread.inbox_id == inbox.id)
                                         .order_by(desc(Thread.last_message_at)).limit(100)))
    att_threads = set(await session.scalars(
        select(Message.thread_id).join(Message.__table__.metadata.tables["attachments"],
                                       Message.__table__.metadata.tables["attachments"].c.message_id == Message.id)
        .where(Message.inbox_id == inbox.id)))
    current = None
    messages: list[dict] = []
    if threads:
        current = next((t for t in threads if t.id == thread), threads[0])
        msgs = list(await session.scalars(select(Message).where(Message.thread_id == current.id)
                                          .order_by(Message.created_at, Message.id)))
        atts = await attachments_for_messages(session, org, [m.id for m in msgs])
        messages = [{"m": m, "html": _clean_html(m.html_body), "attachments": atts[m.id],
                     "edge": "var(--data-blue)" if m.direction == "inbound" else "var(--color-accent-default)",
                     "icon": "arrowDown" if m.direction == "inbound" else "arrowUp",
                     "to": ", ".join(a["email"] for a in m.to_addresses)} for m in msgs]
    policy = await get_effective_policy(session, org, inbox.id)
    row = await get_policy_row(session, org, inbox.id)
    limits = []
    for key, window, label in (("emails_per_minute", timedelta(minutes=1), "per minute"),
                               ("emails_per_hour", timedelta(hours=1), "per hour"),
                               ("emails_per_day", timedelta(days=1), "per day")):
        cap = (policy.get("limits") or {}).get(key)
        used = await session.scalar(select(func.count()).select_from(Message).where(
            Message.inbox_id == inbox.id, Message.direction == "outbound", Message.created_at >= utcnow() - window))
        limits.append({"label": label, "used": used or 0, "max": cap, "pct": min(100, round(100 * (used or 0) / cap)) if cap else 0})
    events = [event_view(e) for e in await session.scalars(
        select(Event).where(Event.organization_id == org,
                            or_(Event.resource_id == inbox.id, Event.payload["inbox_id"].astext == inbox.id))
        .order_by(desc(Event.id)).limit(50))]
    reply_target = next((x["m"] for x in reversed(messages) if x["m"].direction == "inbound"), None) or \
        (messages[-1]["m"] if messages else None)
    ctx.update({"inbox": inbox, "tab": tab, "threads": threads, "att_threads": att_threads, "current": current,
                "messages": messages, "policy": policy, "policy_config": json.dumps(row.config if row else {}, indent=2, ensure_ascii=False),
                "limits": limits, "events": events, "send": send, "reply_target": reply_target,
                "inbox_json": json.dumps(inbox_to_dict(inbox), indent=2, ensure_ascii=False),
                "mode": "ephemeral · " + _ttl_left(inbox.expires_at) if inbox.expires_at else inbox.provider_mode})
    return _render(request, "inbox.html", ctx)


@router.post("/inboxes/{inbox_id}/status")
async def inbox_status(inbox_id: str, action: str = Form(...), principal: Principal = Depends(dash_principal),
                       session: AsyncSession = Depends(get_session)):
    _scope(principal, "inboxes:write")
    inbox = await get_inbox(session, principal.organization_id, inbox_id)
    if action == "delete":
        inbox.status, inbox.deleted_at = "deleted", utcnow()
        await emit(session, organization_id=inbox.organization_id, resource_type="inbox", resource_id=inbox.id,
                   type="inbox.deleted", payload={"inbox_id": inbox.id, "actor": principal.api_key_id})
        await session.commit()
        return _redirect("/dashboard/inboxes", f"Inbox {inbox.address} deleted")
    if inbox.status == "expired":
        raise APIError(409, "inbox_disabled", "Expired inboxes cannot change status.")
    inbox.status = "suspended" if action == "disable" else "active"
    await emit(session, organization_id=inbox.organization_id, resource_type="inbox", resource_id=inbox.id,
               type="inbox.disabled" if action == "disable" else "inbox.enabled",
               payload={"inbox_id": inbox.id, "inbox": inbox_to_dict(inbox), "actor": principal.api_key_id})
    await session.commit()
    return _redirect(f"/dashboard/inboxes/{inbox_id}", f"Inbox {inbox.status}")


@router.post("/inboxes/{inbox_id}/send")
async def inbox_send(inbox_id: str, to: str = Form(...), subject: str = Form(""), text: str = Form(""),
                     principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session),
                     runtime: Runtime = Depends(get_runtime)):
    _scope(principal, "messages:send")
    inbox = await get_inbox(session, principal.organization_id, inbox_id)
    if inbox.status != "active":
        raise APIError(409, "inbox_disabled", "Inbox is not active.")
    draft = OutboundDraft(to=[{"email": e.strip()} for e in to.split(",") if e.strip()], subject=subject, text=text)
    message = await create_outbound_message(session, runtime.storage, runtime.settings,
                                            organization_id=principal.organization_id, inbox=inbox, draft=draft)
    await session.commit()
    return _redirect(f"/dashboard/inboxes/{inbox_id}?thread={message.thread_id}",
                     f"Message {message.status} · {message.id}")


@router.post("/inboxes/{inbox_id}/reply")
async def inbox_reply(inbox_id: str, message_id: str = Form(...), text: str = Form(...), reply_all: str = Form(""),
                      principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session),
                      runtime: Runtime = Depends(get_runtime)):
    _scope(principal, "messages:send")
    inbox = await get_inbox(session, principal.organization_id, inbox_id)
    original = await get_message(session, principal.organization_id, message_id)
    if inbox.status != "active":
        raise APIError(409, "inbox_disabled", "Inbox is not active.")
    if not text.strip():
        return _redirect(f"/dashboard/inboxes/{inbox_id}?thread={original.thread_id}")
    draft = build_reply_draft(original, inbox, text=text, html=None, reply_all=bool(reply_all), to=[], cc=[], bcc=[],
                              attachment_ids=[])
    message = await create_outbound_message(session, runtime.storage, runtime.settings,
                                            organization_id=principal.organization_id, inbox=inbox, draft=draft)
    await session.commit()
    return _redirect(f"/dashboard/inboxes/{inbox_id}?thread={message.thread_id}", f"Reply {message.status} · {message.id}")


@router.post("/inboxes/{inbox_id}/policy")
async def inbox_policy(inbox_id: str, config: str = Form(""), principal: Principal = Depends(dash_principal),
                       session: AsyncSession = Depends(get_session)):
    _scope(principal, "policies:write")
    await get_inbox(session, principal.organization_id, inbox_id)
    await _save_policy_json(session, principal, inbox_id, config)
    return _redirect(f"/dashboard/inboxes/{inbox_id}?tab=policies", "Inbox policy saved")


# ---------------------------------------------------------------- messages / approvals

@router.get("/messages/{message_id}", response_class=HTMLResponse)
async def message_view(request: Request, message_id: str, principal: Principal = Depends(dash_principal),
                       session: AsyncSession = Depends(get_session)):
    m = await get_message(session, principal.organization_id, message_id)
    atts = await attachments_for_messages(session, principal.organization_id, [m.id])
    events = [event_view(e) for e in await session.scalars(select(Event).where(Event.resource_id == m.id).order_by(Event.id))]
    ctx = await _shell(request, session, principal, "inboxes")
    ctx.update({"msg": m, "html": _clean_html(m.html_body), "attachments": atts[m.id], "events": events})
    return _render(request, "message.html", ctx)


@router.post("/messages/{message_id}/approval")
async def message_approval(message_id: str, action: str = Form(...), reason: str = Form(""),
                           principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session)):
    _scope(principal, "approvals:write")
    m = await get_message(session, principal.organization_id, message_id)
    if action == "approve":
        await approve_message(session, m, actor=principal.api_key_id)
    else:
        await reject_message(session, m, actor=principal.api_key_id, reason=reason or None)
    await session.commit()
    return _redirect("/dashboard/approvals", f"Message {m.status} · {m.id}")


@router.get("/approvals", response_class=HTMLResponse)
async def approvals(request: Request, principal: Principal = Depends(dash_principal),
                    session: AsyncSession = Depends(get_session)):
    rows = list(await session.scalars(select(Message).where(Message.organization_id == principal.organization_id,
                                                            Message.status == "pending_approval").order_by(desc(Message.id)).limit(100)))
    ctx = await _shell(request, session, principal, "policies")
    ctx.update({"messages": rows})
    return _render(request, "approvals.html", ctx)


@router.get("/api/messages/{message_id}/attachments/{attachment_id}")
async def attachment_redirect(message_id: str, attachment_id: str, principal: Principal = Depends(dash_principal),
                              session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    att = await get_attachment(session, principal.organization_id, attachment_id)
    url = await runtime.storage.presign_get(att.storage_key, att.filename, PRESIGN_GET_SECONDS)
    return RedirectResponse(url, status_code=302)


# ---------------------------------------------------------------- domains

@router.get("/domains", response_class=HTMLResponse)
async def domains(request: Request, domain: str | None = None, add: str | None = None,
                  principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session),
                  runtime: Runtime = Depends(get_runtime)):
    org = principal.organization_id
    rows = list(await session.scalars(select(Domain).where(
        or_(Domain.organization_id == org, Domain.organization_id.is_(None)), Domain.deleted_at.is_(None))
        .order_by(Domain.organization_id.is_(None), Domain.created_at)))
    counts = dict((await session.execute(select(Inbox.domain_id, func.count()).where(
        Inbox.organization_id == org, Inbox.deleted_at.is_(None)).group_by(Inbox.domain_id))).all())
    current = next((d for d in rows if d.id == domain), rows[0] if rows else None)
    dns = []
    if current is not None and current.type == "customer_custom":
        results = current.check_results or {}
        for r in domain_to_dict(current, runtime.settings)["dns"]:
            st = results.get(r["purpose"])
            if st is None:
                st = "pending"
            elif st == "skipped":
                st = "recommended"
            dns.append({**r, "status": st, "kind": {"ok": "success", "partial": "warning", "missing": "error", "wrong": "error",
                                                    "pending": "warning", "recommended": "default"}.get(st, "default")})
    elif current is not None:
        dns = [{"type": "MX", "name": current.domain, "value": "managed by AgentBox", "status": "valid", "kind": "success"},
               {"type": "TXT", "name": current.domain, "value": "SPF managed", "status": "valid", "kind": "success"}]
    rules = list(await session.scalars(select(RoutingRule).where(RoutingRule.organization_id == org)
                                       .order_by(RoutingRule.priority)))
    accounts = {a.id: a for a in await session.scalars(select(ProviderAccount).where(
        or_(ProviderAccount.organization_id == org, ProviderAccount.organization_id.is_(None)),
        ProviderAccount.status == "active"))}
    routing = [f"{(r.match or {}).get('recipient_domain_suffix') and '*.' + r.match['recipient_domain_suffix'] or 'all'} → "
               f"{accounts[r.provider_account_id].name if r.provider_account_id in accounts else '?'}" for r in rules]
    default = next((a for a in accounts.values() if a.organization_id == org), None) or \
        next((a for a in accounts.values() if a.organization_id is None), None)
    ctx = await _shell(request, session, principal, "domains")
    ctx.update({"domains": rows, "counts": counts, "current": current, "dns": dns, "add": add, "routing": routing,
                "default_provider": f"{default.provider} · {default.name}" if default else "none configured",
                "mx_hosts": runtime.settings.mx_hostnames.replace(",", ", "), "recheck": runtime.settings.domain_recheck_pending_seconds // 60})
    return _render(request, "domains.html", ctx)


@router.post("/domains")
async def domain_add(domain: str = Form(...), principal: Principal = Depends(dash_principal),
                     session: AsyncSession = Depends(get_session)):
    _scope(principal, "domains:write")
    from agentbox.api.schemas import DomainCreate

    try:
        name = DomainCreate(domain=domain).domain
    except ValidationError as e:
        raise APIError(422, "validation_error", "Invalid domain name.") from e
    existing = await session.scalar(select(Domain).where(Domain.domain == name, Domain.deleted_at.is_(None)))
    if existing is not None:
        raise APIError(409, "conflict", "Domain already registered.")
    d = Domain(id=new_id("dom"), organization_id=principal.organization_id, domain=name, type="customer_custom",
               status="verification_pending", verification_token=secrets.token_urlsafe(24))
    session.add(d)
    await session.flush()
    await emit(session, organization_id=d.organization_id, resource_type="domain", resource_id=d.id,
               type="domain.verification_pending", payload={"domain_id": d.id, "domain": domain_to_dict(d)})
    await enqueue(session, "domain_verify", {"domain_id": d.id})
    await session.commit()
    return _redirect(f"/dashboard/domains?domain={d.id}", f"Domain {name} added · publish the DNS records")


@router.post("/domains/{domain_id}/action")
async def domain_action(domain_id: str, action: str = Form(...), principal: Principal = Depends(dash_principal),
                        session: AsyncSession = Depends(get_session)):
    _scope(principal, "domains:write")
    d = await session.scalar(select(Domain).where(Domain.id == domain_id, Domain.deleted_at.is_(None),
                                                  Domain.organization_id == principal.organization_id))
    if d is None:
        raise APIError(404, "not_found", "Domain not found.")
    if action == "verify":
        await enqueue(session, "domain_verify", {"domain_id": d.id})
        await session.commit()
        return _redirect(f"/dashboard/domains?domain={d.id}", "DNS re-check queued")
    if action == "delete":
        used = await session.scalar(select(func.count()).select_from(Inbox).where(Inbox.domain_id == d.id,
                                                                                  Inbox.deleted_at.is_(None)))
        if used:
            raise APIError(409, "conflict", "Domain still has inboxes.")
        d.deleted_at, d.status = utcnow(), "deleted"
        await session.commit()
        return _redirect("/dashboard/domains", f"Domain {d.domain} deleted")
    return _redirect(f"/dashboard/domains?domain={d.id}")


# ---------------------------------------------------------------- webhooks

@router.get("/webhooks", response_class=HTMLResponse)
async def webhooks(request: Request, hook: str | None = None, create: str | None = None,
                   principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session)):
    org = principal.organization_id
    rows = list(await session.scalars(select(Webhook).where(Webhook.organization_id == org, Webhook.deleted_at.is_(None))
                                      .order_by(Webhook.created_at)))
    health = {h["id"]: h for h in await _webhook_health(session, org)}
    current = next((w for w in rows if w.id == hook), rows[0] if rows else None)
    attempts = []
    if current is not None:
        deliveries = list(await session.scalars(select(WebhookDelivery).where(WebhookDelivery.webhook_id == current.id)
                                                .order_by(desc(WebhookDelivery.id)).limit(60)))
        types = dict((await session.execute(select(Event.id, Event.type).where(
            Event.id.in_([d.event_id for d in deliveries])))).all()) if deliveries else {}
        for d in deliveries:
            ms = f"{int((d.finished_at - d.started_at).total_seconds() * 1000)} ms" if d.started_at and d.finished_at else "—"
            code = str(d.response_status) if d.response_status else (d.error or d.status)
            attempts.append({"d": d, "type": types.get(d.event_id, "?"), "ms": ms, "code": code,
                             "kind": "success" if d.status == "succeeded" else ("info" if d.status == "pending" else "error"),
                             "can_retry": d.status in ("failed", "exhausted")})
    ctx = await _shell(request, session, principal, "webhooks")
    ctx.update({"webhooks": rows, "health": health, "current": current, "attempts": attempts, "create": create,
                "new_secret": request.query_params.get("secret")})
    return _render(request, "webhooks.html", ctx)


@router.post("/webhooks")
async def webhook_add(url: str = Form(...), event_types: str = Form("*"), description: str = Form(""),
                      principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session),
                      runtime: Runtime = Depends(get_runtime)):
    _scope(principal, "webhooks:write")
    if not url.startswith(("http://", "https://")):
        raise APIError(422, "validation_error", "URL must start with http:// or https://")
    secret = generate_secret()
    types = [t.strip() for t in event_types.replace("\n", ",").split(",") if t.strip()] or ["*"]
    hook = Webhook(id=new_id("whk"), organization_id=principal.organization_id, url=url.strip(),
                   secret_encrypted=encrypt_str(runtime.settings.app_secret_key, secret), status="active",
                   event_types=types, description=description.strip() or None)
    session.add(hook)
    await session.commit()
    return RedirectResponse(f"/dashboard/webhooks?hook={hook.id}&secret={secret}", status_code=303)


@router.post("/webhooks/{webhook_id}/action")
async def webhook_action(webhook_id: str, action: str = Form(...), delivery_id: str = Form(""),
                         principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session),
                         runtime: Runtime = Depends(get_runtime)):
    _scope(principal, "webhooks:write")
    hook = await session.scalar(select(Webhook).where(Webhook.id == webhook_id,
                                                      Webhook.organization_id == principal.organization_id))
    if hook is None:
        raise APIError(404, "not_found", "Webhook not found.")
    toast = None
    if action == "retry" and delivery_id:
        original = await session.get(WebhookDelivery, delivery_id)
        if original is not None and original.webhook_id == hook.id and original.status != "pending":
            last = await session.scalar(select(func.max(WebhookDelivery.attempt_number)).where(
                WebhookDelivery.webhook_id == hook.id, WebhookDelivery.event_id == original.event_id))
            nxt = WebhookDelivery(id=new_id("wdl"), organization_id=hook.organization_id, webhook_id=hook.id,
                                  event_id=original.event_id, attempt_number=(last or 0) + 1, status="pending",
                                  scheduled_at=utcnow())
            session.add(nxt)
            await session.flush()
            await enqueue(session, "webhook_deliver", {"delivery_id": nxt.id})
            toast = f"Redelivery queued · attempt {nxt.attempt_number}"
    elif action == "test":
        event = Event(id=new_id("evt"), organization_id=hook.organization_id, resource_type="webhook", resource_id=hook.id,
                      type="webhook.test", payload={"webhook_id": hook.id, "message": "This is a test event from the AgentBox console."})
        session.add(event)
        await session.flush()
        d = WebhookDelivery(id=new_id("wdl"), organization_id=hook.organization_id, webhook_id=hook.id, event_id=event.id,
                            attempt_number=1, status="pending", scheduled_at=utcnow())
        session.add(d)
        await session.flush()
        await enqueue(session, "webhook_deliver", {"delivery_id": d.id})
        toast = f"Test event {event.id} queued"
    elif action == "rotate":
        secret = generate_secret()
        hook.secret_encrypted = encrypt_str(runtime.settings.app_secret_key, secret)
        await session.commit()
        return RedirectResponse(f"/dashboard/webhooks?hook={hook.id}&secret={secret}", status_code=303)
    elif action == "toggle":
        hook.status = "disabled" if hook.status == "active" else "active"
        toast = f"Webhook {hook.status}"
    elif action == "delete":
        hook.deleted_at, hook.status = utcnow(), "disabled"
        await session.commit()
        return _redirect("/dashboard/webhooks", "Webhook deleted")
    await session.commit()
    return _redirect(f"/dashboard/webhooks?hook={webhook_id}", toast)


# ---------------------------------------------------------------- api keys

@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys(request: Request, create: str | None = None, principal: Principal = Depends(dash_principal),
                   session: AsyncSession = Depends(get_session)):
    rows = list(await session.scalars(select(ApiKey).where(ApiKey.organization_id == principal.organization_id)
                                      .order_by(ApiKey.created_at)))
    ctx = await _shell(request, session, principal, "keys")
    ctx.update({"keys": rows, "scopes": ALL_SCOPES, "new_key": request.query_params.get("new_key"), "create": create})
    return _render(request, "api_keys.html", ctx)


@router.post("/api-keys")
async def api_key_add(request: Request, name: str = Form(...), environment: str = Form("live"),
                      principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session)):
    _scope(principal, "keys:write")
    form = await request.form()
    scopes = tuple(s for s in form.getlist("scopes") if s in ALL_SCOPES) or ("admin",)
    if not principal.has("admin") and any(not principal.has(s) for s in scopes):
        raise APIError(403, "forbidden", "Cannot grant scopes you do not hold.")
    key, plaintext = await create_api_key(session, principal.organization_id, name=name.strip() or "key", scopes=scopes,
                                          environment="test" if environment == "test" else "live")
    await emit(session, organization_id=principal.organization_id, resource_type="api_key", resource_id=key.id,
               type="api_key.created", payload={"name": key.name, "scopes": key.scopes, "actor": principal.api_key_id})
    await session.commit()
    return RedirectResponse(f"/dashboard/api-keys?new_key={plaintext}", status_code=303)


@router.post("/api-keys/{key_id}/revoke")
async def api_key_revoke(key_id: str, principal: Principal = Depends(dash_principal),
                         session: AsyncSession = Depends(get_session)):
    _scope(principal, "keys:write")
    key = await session.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.organization_id == principal.organization_id))
    if key is not None and key.id != principal.api_key_id and key.revoked_at is None:
        key.revoked_at = utcnow()
        await emit(session, organization_id=principal.organization_id, resource_type="api_key", resource_id=key.id,
                   type="api_key.revoked", payload={"name": key.name, "actor": principal.api_key_id})
        await session.commit()
        return _redirect("/dashboard/api-keys", f"Key {key.name} revoked")
    return _redirect("/dashboard/api-keys")


# ---------------------------------------------------------------- usage

@router.get("/usage", response_class=HTMLResponse)
async def usage(request: Request, principal: Principal = Depends(dash_principal),
                session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    usage_path = registry().paths(runtime.settings)["usage"]
    if usage_path != "/dashboard/usage":
        return RedirectResponse(usage_path, status_code=303)
    org = principal.organization_id
    rows = list(await session.scalars(select(UsageDaily).where(UsageDaily.organization_id == org)
                                      .order_by(desc(UsageDaily.day)).limit(31)))
    live = await compute_usage(session, org, utcnow().date())
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).date()
    month_rows = [r for r in rows if r.day >= month_start]
    sent_month = sum(r.messages_sent for r in month_rows if r.day != utcnow().date()) + live["messages_sent"]
    received_month = sum(r.messages_received for r in month_rows if r.day != utcnow().date()) + live["messages_received"]
    meters = [
        {"label": "Active inboxes", "used": live["active_inboxes"], "max": "unlimited", "pct": 0, "color": "var(--color-accent-default)"},
        {"label": "Emails sent · this month", "used": sent_month, "max": "unlimited", "pct": 0, "color": "var(--color-accent-default)"},
        {"label": "Emails received · this month", "used": received_month, "max": "unlimited", "pct": 0, "color": "var(--data-blue)"},
        {"label": "Attachment storage", "used": f"{live['attachment_bytes_stored'] / 1048576:.1f} MB", "max": "unlimited", "pct": 0,
         "color": "var(--data-teal)"},
        {"label": "Custom domains", "used": live["custom_domains"], "max": "unlimited", "pct": 0, "color": "var(--data-violet)"},
    ]
    peak = max([1] + [max(r.messages_sent, r.messages_received) for r in rows])
    for m, val in zip(meters, [live["active_inboxes"], sent_month, received_month, live["attachment_bytes_stored"],
                               live["custom_domains"]], strict=False):
        m["pct"] = min(100, round(100 * val / max(1, val))) if val else 0
    ctx = await _shell(request, session, principal, "usage")
    ctx.update({"rows": rows, "live": live, "meters": meters, "peak": peak, "month_start": month_start,
                "period": f"{month_start.strftime('%-d %b')} – {utcnow().strftime('%-d %b %Y')}"})
    return _render(request, "usage.html", ctx)


# ---------------------------------------------------------------- policies

async def _save_policy_json(session: AsyncSession, principal: Principal, inbox_id: str | None, config: str) -> None:
    try:
        raw = json.loads(config or "{}")
        cfg = validate_policy_config(raw)
    except (ValueError, ValidationError) as e:
        raise APIError(422, "validation_error", f"Invalid policy JSON: {e}") from e
    await _save_policy(session, principal, inbox_id, cfg)


async def _save_policy(session: AsyncSession, principal: Principal, inbox_id: str | None, cfg: dict) -> None:
    await set_policy(session, principal.organization_id, inbox_id, cfg)
    await emit(session, organization_id=principal.organization_id, resource_type="policy",
               resource_id=inbox_id or principal.organization_id, type="policy.changed",
               payload={"inbox_id": inbox_id, "config": cfg, "actor": principal.api_key_id})
    await session.commit()


@router.get("/policies", response_class=HTMLResponse)
async def policies(request: Request, scope: str = "org", principal: Principal = Depends(dash_principal),
                   session: AsyncSession = Depends(get_session)):
    org = principal.organization_id
    inbox_id = None if scope == "org" else scope
    inbox = await get_inbox(session, org, inbox_id) if inbox_id else None
    row = await get_policy_row(session, org, inbox_id)
    effective = await get_effective_policy(session, org, inbox_id)
    inboxes_list = list(await session.scalars(select(Inbox).where(Inbox.organization_id == org, Inbox.deleted_at.is_(None))
                                              .order_by(Inbox.address).limit(200)))
    sups = list(await session.scalars(select(Suppression).where(Suppression.organization_id == org)
                                      .order_by(desc(Suppression.created_at)).limit(200)))
    ctx = await _shell(request, session, principal, "policies")
    ctx.update({"scope": scope, "inbox": inbox, "inboxes_list": inboxes_list, "effective": effective,
                "config": row.config if row else {}, "config_json": json.dumps(row.config if row else {}, indent=2, ensure_ascii=False),
                "effective_json": json.dumps(effective, indent=2, ensure_ascii=False),
                "defaults_json": json.dumps(DEFAULT_POLICY, indent=2), "suppressions": sups})
    return _render(request, "policies.html", ctx)


@router.post("/policies")
async def policies_save(request: Request, scope: str = Form("org"), mode: str = Form("form"), config: str = Form(""),
                        principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session)):
    _scope(principal, "policies:write")
    inbox_id = None if scope == "org" else scope
    if inbox_id:
        await get_inbox(session, principal.organization_id, inbox_id)
    if mode == "json":
        await _save_policy_json(session, principal, inbox_id, config)
        return _redirect(f"/dashboard/policies?scope={scope}", "Policy saved")
    form = await request.form()

    def domains(name: str) -> list[str]:
        return [d.strip().lower().lstrip("@") for d in (form.get(name) or "").replace("\n", ",").split(",") if d.strip()]

    def num(name: str) -> int | None:
        v = (form.get(name) or "").strip()
        return int(v) if v.isdigit() else None

    cfg: dict = {
        "send_enabled": form.get("send_enabled") == "on", "receive_enabled": form.get("receive_enabled") == "on",
        "recipient_policy": {"allowed_domains": domains("allowed_domains"), "blocked_domains": domains("blocked_domains")},
        "limits": {k: v for k, v in {"emails_per_minute": num("emails_per_minute"), "emails_per_hour": num("emails_per_hour"),
                                     "emails_per_day": num("emails_per_day"),
                                     "per_thread_per_hour": num("per_thread_per_hour") if form.get("loop_protection") == "on" else 0}.items()
                   if v is not None},
        "attachments": {"allow_executables": form.get("allow_executables") == "on",
                        **({"max_size_mb": num("max_size_mb")} if num("max_size_mb") is not None else {})},
        "approval": {"new_recipient": form.get("approval_new_recipient") == "on",
                     "external_domain": form.get("approval_external_domain") == "on"},
    }
    try:
        cfg = validate_policy_config(cfg)
    except ValidationError as e:
        raise APIError(422, "validation_error", "Invalid policy values.", {"errors": e.errors()}) from e
    await _save_policy(session, principal, inbox_id, cfg)
    return _redirect(f"/dashboard/policies?scope={scope}", "Policy saved")


@router.post("/suppressions")
async def suppression_add(email: str = Form(...), note: str = Form(""), principal: Principal = Depends(dash_principal),
                          session: AsyncSession = Depends(get_session)):
    _scope(principal, "suppressions:write")
    await add_suppression(session, organization_id=principal.organization_id, email=email, reason="manual", note=note or None)
    await session.commit()
    return _redirect("/dashboard/policies", f"{email.strip().lower()} suppressed")


@router.post("/suppressions/{suppression_id}/delete")
async def suppression_delete(suppression_id: str, principal: Principal = Depends(dash_principal),
                             session: AsyncSession = Depends(get_session)):
    _scope(principal, "suppressions:write")
    row = await session.scalar(select(Suppression).where(Suppression.id == suppression_id,
                                                         Suppression.organization_id == principal.organization_id))
    if row is not None:
        await session.delete(row)
        await session.commit()
    return _redirect("/dashboard/policies", "Suppression removed")


# ---------------------------------------------------------------- audit

AUDIT_FILTERS = {"all": None, "keys": "api_key.", "policies": ("policy.", "approval.", "suppression."),
                 "messages": "message.", "domains": "domain.", "inboxes": "inbox.", "webhooks": "webhook."}


def _audit_stmt(org: str, filter_: str, type_: str | None):
    stmt = select(Event).where(Event.organization_id == org)
    prefix = AUDIT_FILTERS.get(filter_)
    if isinstance(prefix, tuple):
        stmt = stmt.where(or_(*[Event.type.like(p + "%") for p in prefix]))
    elif prefix:
        stmt = stmt.where(Event.type.like(prefix + "%"))
    if type_:
        stmt = stmt.where(Event.type == type_)
    return stmt.order_by(desc(Event.id))


@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request, filter: str = "all", type: str | None = None,
                principal: Principal = Depends(dash_principal), session: AsyncSession = Depends(get_session)):
    rows = [event_view(e) for e in await session.scalars(_audit_stmt(principal.organization_id, filter, type).limit(200))]
    ctx = await _shell(request, session, principal, "audit")
    ctx.update({"events": rows, "filter": filter, "type": type or "", "filters": list(AUDIT_FILTERS)})
    return _render(request, "audit.html", ctx)


@router.get("/audit.csv")
async def audit_csv(filter: str = "all", principal: Principal = Depends(dash_principal),
                    session: AsyncSession = Depends(get_session)):
    rows = list(await session.scalars(_audit_stmt(principal.organization_id, filter, None).limit(5000)))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "type", "resource_type", "resource_id", "actor", "payload"])
    for e in rows:
        w.writerow([e.created_at.isoformat(), e.type, e.resource_type, e.resource_id, (e.payload or {}).get("actor", ""),
                    json.dumps(e.payload, ensure_ascii=False)])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="agentbox-audit.csv"'})


# ---------------------------------------------------------------- quickstart / console / search

@router.get("/quickstart", response_class=HTMLResponse)
async def quickstart(request: Request, principal: Principal = Depends(dash_principal),
                     session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    funnel = {f["label"]: f["state"] == "done" for f in await _funnel(session, principal.organization_id)}
    base = runtime.settings.api_base_url
    steps = [
        {"n": 1, "title": "Install the SDK", "sub": "Python or TypeScript. MCP works without any code.", "done": True,
         "code": "pip install ./sdk/python            # agentbox-sdk\n# or\ncd sdk/typescript && npm install && npm run build"},
        {"n": 2, "title": "Create an inbox", "sub": f"On the shared {runtime.settings.managed_domain} domain — no DNS needed.",
         "done": funnel.get("First inbox created", False),
         "code": f'mail = AgentBox("ab_live_...", base_url="{base}")\ninbox = mail.inboxes.create("procurement-agent")\n# procurement-agent@{runtime.settings.managed_domain}'},
        {"n": 3, "title": "Send and get lifecycle events", "sub": "Returns queued immediately; delivery arrives as events.",
         "done": funnel.get("First email sent", False),
         "code": 'mail.messages.send(inbox["id"], to=["sales@supplier.ru"],\n    subject="Запрос КП", text="Пришлите КП.")\n# message.queued → provider_accepted → delivered'},
        {"n": 4, "title": "Receive replies via webhook or long-poll", "sub": "No IMAP polling. Verify the signature, dedupe by event.id.",
         "done": funnel.get("Custom webhook registered", False),
         "code": '@app.post("/agentbox/events")\nasync def on_event(event):\n    if event["type"] == "message.received":\n        await agent.run(event["data"]["message"])\n\n# or simply: mail.messages.wait_for(inbox["id"], timeout=300)'},
    ]
    ctx = await _shell(request, session, principal, "quickstart")
    ctx.update({"steps": steps, "base_url": base, "smtp_port": runtime.settings.smtp_bind_port,
                "managed_domain": runtime.settings.managed_domain})
    return _render(request, "quickstart.html", ctx)


CONSOLE_ENDPOINTS = [
    {"method": "POST", "path": "/v1/inboxes", "desc": "Create inbox",
     "body": '{\n  "username": "rfq-agent",\n  "metadata": { "agent_id": "agent_7cf3" }\n}'},
    {"method": "POST", "path": "/v1/inboxes/{id}/messages", "desc": "Send",
     "body": '{\n  "to": [{ "email": "sales@supplier.ru" }],\n  "subject": "Запрос КП",\n  "text": "Пришлите, пожалуйста, коммерческое предложение."\n}'},
    {"method": "POST", "path": "/v1/messages/{id}/reply", "desc": "Reply in thread",
     "body": '{\n  "text": "Спасибо. Уточните срок поставки.",\n  "reply_all": true\n}'},
    {"method": "GET", "path": "/v1/inboxes/{id}/messages?direction=inbound&wait=10", "desc": "Long-poll inbound", "body": ""},
    {"method": "GET", "path": "/v1/threads/{id}", "desc": "Thread with messages", "body": ""},
    {"method": "GET", "path": "/v1/attachments/{id}/download", "desc": "Signed URL", "body": ""},
]


def _snippets(ep: dict, path: str, body: str, base: str) -> dict[str, str]:
    compact = " ".join(body.split()) if body.strip() else ""
    curl = f'curl -X {ep["method"]} {base}{path} \\\n  -H "Authorization: Bearer $AGENTBOX_API_KEY" \\\n  -H "Idempotency-Key: $(uuidgen)"'
    if compact:
        curl += f" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{compact}'"
    py = {
        "/v1/inboxes": 'from agentbox_sdk import AgentBox\n\nmail = AgentBox("ab_live_...", base_url="' + base + '")\ninbox = mail.inboxes.create("rfq-agent", metadata={"agent_id": "agent_7cf3"})\nprint(inbox["email"])',
        "/v1/inboxes/{id}/messages": 'msg = mail.messages.send(\n    "ibx_...",\n    to=["sales@supplier.ru"],\n    subject="Запрос КП",\n    text="Пришлите, пожалуйста, коммерческое предложение.",\n)\nprint(msg["status"])  # queued',
        "/v1/messages/{id}/reply": 'mail.messages.reply("msg_...", text="Спасибо. Уточните срок поставки.", reply_all=True)',
        "/v1/inboxes/{id}/messages?direction=inbound&wait=10": 'reply = mail.messages.wait_for("ibx_...", timeout=300)\nprint(reply["text"] if reply else "no reply yet")',
        "/v1/threads/{id}": 'thread = mail.threads.get("thr_...")\nfor m in thread["messages"]:\n    print(m["direction"], m["from"]["email"], m["subject"])',
        "/v1/attachments/{id}/download": 'url = mail.attachments.download_url("att_...")["url"]\n# short-lived signed URL, 600 s',
    }[ep["path"]]
    ts = {
        "/v1/inboxes": 'import { AgentBox } from "@agentbox/sdk";\n\nconst mail = new AgentBox({ apiKey: process.env.AGENTBOX_API_KEY!, baseUrl: "' + base + '" });\nconst inbox = await mail.inboxes.create({ username: "rfq-agent", metadata: { agent_id: "agent_7cf3" } });\nconsole.log(inbox.email);',
        "/v1/inboxes/{id}/messages": 'const msg = await mail.messages.send("ibx_...", {\n  to: ["sales@supplier.ru"],\n  subject: "Запрос КП",\n  text: "Пришлите, пожалуйста, коммерческое предложение.",\n});\nconsole.log(msg.status); // queued',
        "/v1/messages/{id}/reply": 'await mail.messages.reply("msg_...", { text: "Спасибо. Уточните срок поставки.", reply_all: true });',
        "/v1/inboxes/{id}/messages?direction=inbound&wait=10": 'const reply = await mail.messages.waitFor("ibx_...", { timeoutMs: 300_000 });',
        "/v1/threads/{id}": 'const thread = await mail.threads.get("thr_...");\nfor (const m of thread.messages) console.log(m.direction, m.from.email, m.subject);',
        "/v1/attachments/{id}/download": 'const { url } = await mail.attachments.downloadUrl("att_...");',
    }[ep["path"]]
    return {"curl": curl, "python": py, "ts": ts}


async def _console_ctx(request: Request, session: AsyncSession, principal: Principal, runtime: Runtime, ep: int,
                       lang: str, resource_id: str, body: str | None) -> dict:
    endpoint = CONSOLE_ENDPOINTS[max(0, min(ep, len(CONSOLE_ENDPOINTS) - 1))]
    if not resource_id:
        if "/inboxes/{id}" in endpoint["path"]:
            resource_id = await session.scalar(select(Inbox.id).where(Inbox.organization_id == principal.organization_id,
                                                                    Inbox.deleted_at.is_(None)).order_by(desc(Inbox.id)).limit(1)) or ""
        elif "/messages/{id}" in endpoint["path"]:
            resource_id = await session.scalar(select(Message.id).where(Message.organization_id == principal.organization_id)
                                               .order_by(desc(Message.id)).limit(1)) or ""
        elif "/threads/{id}" in endpoint["path"]:
            resource_id = await session.scalar(select(Thread.id).where(Thread.organization_id == principal.organization_id)
                                               .order_by(desc(Thread.id)).limit(1)) or ""
        elif "/attachments/{id}" in endpoint["path"]:
            from agentbox.db.models import Attachment

            resource_id = await session.scalar(select(Attachment.id).where(Attachment.organization_id == principal.organization_id)
                                               .order_by(desc(Attachment.id)).limit(1)) or ""
    path = endpoint["path"].replace("{id}", resource_id or "{id}")
    body = endpoint["body"] if body is None else body
    ctx = await _shell(request, session, principal, "console")
    ctx.update({"endpoints": CONSOLE_ENDPOINTS, "ep": CONSOLE_ENDPOINTS.index(endpoint), "endpoint": endpoint, "path": path,
                "resource_id": resource_id or "", "body": body, "lang": lang,
                "snippets": _snippets(endpoint, path, body, runtime.settings.api_base_url), "idem": uuid.uuid4().hex[:8] + "…",
                "base_url": runtime.settings.api_base_url, "needs_id": "{id}" in endpoint["path"]})
    return ctx


@router.get("/console", response_class=HTMLResponse)
async def console(request: Request, ep: int = 0, lang: str = Query("curl", pattern="^(curl|python|ts)$"),
                  resource_id: str = "", principal: Principal = Depends(dash_principal),
                  session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    ctx = await _console_ctx(request, session, principal, runtime, ep, lang, resource_id, None)
    ctx.update({"ran": False})
    return _render(request, "console.html", ctx)


@router.post("/console/run", response_class=HTMLResponse)
async def console_run(request: Request, ep: int = Form(0), lang: str = Form("curl"), resource_id: str = Form(""),
                      body: str = Form(""), principal: Principal = Depends(dash_principal),
                      session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    ctx = await _console_ctx(request, session, principal, runtime, ep, lang, resource_id, body)
    endpoint, path = ctx["endpoint"], ctx["path"]
    api_key = decrypt_str(runtime.settings.app_secret_key, request.cookies.get(COOKIE, ""))
    headers = {"Authorization": f"Bearer {api_key}", "Idempotency-Key": uuid.uuid4().hex}
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app), base_url="http://console.local",
                                 timeout=60.0) as client:
        if endpoint["method"] == "GET":
            resp = await client.get(path, headers=headers)
        else:
            try:
                payload = json.loads(body or "{}")
            except ValueError as e:
                raise APIError(422, "validation_error", f"Request body is not valid JSON: {e}") from e
            resp = await client.post(path, headers=headers, json=payload)
    ms = int((time.monotonic() - started) * 1000)
    try:
        pretty = json.dumps(resp.json(), indent=2, ensure_ascii=False)
    except ValueError:
        pretty = resp.text
    ctx.update({"ran": True, "status": resp.status_code, "reason": resp.reason_phrase, "ms": ms,
                "request_id": resp.headers.get("AgentBox-Request-Id", ""), "response": pretty,
                "ok": resp.status_code < 400})
    return _render(request, "console.html", ctx)


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", principal: Principal = Depends(dash_principal),
                 session: AsyncSession = Depends(get_session)):
    org = principal.organization_id
    term = q.strip()
    results = {"inboxes": [], "threads": [], "messages": []}
    if term:
        results["inboxes"] = list(await session.scalars(select(Inbox).where(
            Inbox.organization_id == org, Inbox.deleted_at.is_(None), Inbox.address.ilike(f"%{term}%")).limit(20)))
        results["threads"] = list(await session.scalars(select(Thread).where(
            Thread.organization_id == org, or_(Thread.subject.ilike(f"%{term}%"), Thread.id == term)).limit(20)))
        results["messages"] = list(await session.scalars(select(Message).where(
            Message.organization_id == org, or_(Message.id == term, Message.internet_message_id.ilike(f"%{term}%"),
                                                Message.subject.ilike(f"%{term}%"))).order_by(desc(Message.id)).limit(20)))
    ctx = await _shell(request, session, principal, "search")
    ctx.update({"q": term, "results": results})
    return _render(request, "search.html", ctx)


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#ea6464"/>'
           '<g fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" transform="translate(4 4) scale(.67)">'
           '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></g></svg>')


@router.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- error handlers

def register_dashboard_handlers(app) -> None:
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        login_path = registry().paths(request.app.state.runtime.settings)["login"]
        return RedirectResponse(login_path + "?next=" + quote(request.url.path) if login_path != "/dashboard/login"
                                else login_path, status_code=303)

    @app.exception_handler(APIError)
    async def _dashboard_api_error(request: Request, exc: APIError):
        from agentbox.api.errors import error_response

        if request.url.path.startswith("/dashboard"):
            ctx = {"page": "error", "principal": None, "icons": ICONS, "theme": request.cookies.get(THEME_COOKIE, "dark"),
                   "nav": NAV, "nav2": NAV2, "error": exc, "status": exc.status, "toast": None, "path": request.url.path,
                   "now": utcnow(), "badge": badge, "ago": ago, "hhmm": hhmm}
            return _render(request, "error.html", ctx, status_code=exc.status)
        return error_response(request, exc.status, exc.code, exc.message, exc.details)


__all__ = ["router", "register_dashboard_handlers", "Response"]
