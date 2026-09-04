from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Attachment, Domain, Inbox, Message, Policy, Suppression, utcnow
from agentbox.domain.addresses import split_address
from agentbox.domain.ids import new_id

DEFAULT_POLICY: dict[str, Any] = {
    "send_enabled": True,
    "receive_enabled": True,
    "recipient_policy": {"allowed_domains": [], "blocked_domains": []},
    "limits": {"emails_per_minute": 60, "emails_per_hour": 500, "emails_per_day": 2000, "per_thread_per_hour": 30},
    "attachments": {"max_size_mb": 20, "allow_executables": False},
    "approval": {"new_recipient": False, "external_domain": False},
}

BLOCKED_EXTENSIONS = frozenset({
    "exe", "dll", "bat", "cmd", "com", "scr", "msi", "ps1", "vbs", "vbe", "js", "jse", "wsf", "jar", "sh", "pif",
    "cpl", "hta", "reg",
})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecipientPolicy(_Strict):
    allowed_domains: list[str] | None = None
    blocked_domains: list[str] | None = None


class LimitsPolicy(_Strict):
    emails_per_minute: int | None = Field(default=None, ge=0)
    emails_per_hour: int | None = Field(default=None, ge=0)
    emails_per_day: int | None = Field(default=None, ge=0)
    per_thread_per_hour: int | None = Field(default=None, ge=0)


class AttachmentsPolicy(_Strict):
    max_size_mb: int | None = Field(default=None, ge=0, le=25)
    allow_executables: bool | None = None


class ApprovalPolicy(_Strict):
    new_recipient: bool | None = None
    external_domain: bool | None = None


class PolicyConfig(_Strict):
    send_enabled: bool | None = None
    receive_enabled: bool | None = None
    recipient_policy: RecipientPolicy | None = None
    limits: LimitsPolicy | None = None
    attachments: AttachmentsPolicy | None = None
    approval: ApprovalPolicy | None = None


def validate_policy_config(raw: dict) -> dict:
    cfg = PolicyConfig.model_validate(raw).model_dump(exclude_unset=True, exclude_none=True)
    rp = cfg.get("recipient_policy") or {}
    for key in ("allowed_domains", "blocked_domains"):
        if key in rp:
            rp[key] = sorted({d.strip().lower().lstrip("@") for d in rp[key] if d.strip()})
    return cfg


def merge_policy(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_policy(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


async def get_policy_row(session: AsyncSession, organization_id: str, inbox_id: str | None) -> Policy | None:
    stmt = select(Policy).where(Policy.organization_id == organization_id)
    stmt = stmt.where(Policy.inbox_id.is_(None)) if inbox_id is None else stmt.where(Policy.inbox_id == inbox_id)
    return await session.scalar(stmt)


async def get_effective_policy(session: AsyncSession, organization_id: str, inbox_id: str | None) -> dict:
    effective = copy.deepcopy(DEFAULT_POLICY)
    org_row = await get_policy_row(session, organization_id, None)
    if org_row is not None:
        effective = merge_policy(effective, org_row.config)
    if inbox_id is not None:
        inbox_row = await get_policy_row(session, organization_id, inbox_id)
        if inbox_row is not None:
            effective = merge_policy(effective, inbox_row.config)
    return effective


async def set_policy(session: AsyncSession, organization_id: str, inbox_id: str | None, config: dict) -> Policy:
    row = await get_policy_row(session, organization_id, inbox_id)
    if row is None:
        row = Policy(id=new_id("pol"), organization_id=organization_id, inbox_id=inbox_id, config=config)
        session.add(row)
    else:
        row.config = config
    await session.flush()
    return row


def _domain_matches(domain: str, patterns: list[str]) -> bool:
    return any(domain == p or domain.endswith("." + p) for p in patterns)


@dataclass
class Block:
    http_status: int
    code: str
    message: str
    reason: str
    details: dict = field(default_factory=dict)


@dataclass
class SendDecision:
    policy: dict
    block: Block | None = None
    approval_reasons: list[str] = field(default_factory=list)

    @property
    def approval_required(self) -> bool:
        return bool(self.approval_reasons)


async def _count_outbound(session: AsyncSession, inbox_id: str, window: timedelta, thread_id: str | None = None) -> int:
    stmt = select(func.count()).select_from(Message).where(
        Message.inbox_id == inbox_id, Message.direction == "outbound", Message.created_at >= utcnow() - window,
        Message.status != "rejected",
    )
    if thread_id:
        stmt = stmt.where(Message.thread_id == thread_id)
    return int(await session.scalar(stmt) or 0)


async def _org_domains(session: AsyncSession, organization_id: str) -> set[str]:
    rows = await session.scalars(
        select(Domain.domain).where(or_(Domain.organization_id == organization_id, Domain.organization_id.is_(None)),
                                    Domain.deleted_at.is_(None))
    )
    return {d.lower() for d in rows}


async def _is_known_recipient(session: AsyncSession, organization_id: str, email: str) -> bool:
    probe = [{"email": email}]
    stmt = select(Message.id).where(
        Message.organization_id == organization_id, Message.direction == "outbound",
        Message.status.not_in(["pending_approval", "rejected"]),
        or_(Message.to_addresses.contains(probe), Message.cc_addresses.contains(probe),
            Message.bcc_addresses.contains(probe)),
    ).limit(1)
    return (await session.scalar(stmt)) is not None


async def evaluate_send(
    session: AsyncSession, *, organization_id: str, inbox: Inbox, recipients: list[dict],
    attachments: list[Attachment], thread_id: str | None,
) -> SendDecision:
    """Suppressions, recipient policy, limits, attachment rules, loop protection, approval gates."""
    policy = await get_effective_policy(session, organization_id, inbox.id)
    decision = SendDecision(policy=policy)
    emails = sorted({(r.get("email") or "").lower() for r in recipients if r.get("email")})

    if not policy.get("send_enabled", True):
        decision.block = Block(409, "inbox_disabled", "Sending is disabled by policy.", "send_disabled")
        return decision

    suppressed = list(await session.scalars(
        select(Suppression.email).where(Suppression.organization_id == organization_id, Suppression.email.in_(emails),
                                        or_(Suppression.expires_at.is_(None), Suppression.expires_at > utcnow()))
    ))
    if suppressed:
        decision.block = Block(422, "suppressed_recipient", "One or more recipients are suppressed.",
                               "suppressed_recipient", {"recipients": sorted(suppressed)})
        return decision

    rp = policy.get("recipient_policy") or {}
    allowed, blocked = rp.get("allowed_domains") or [], rp.get("blocked_domains") or []
    for email in emails:
        _, domain = split_address(email)
        if blocked and _domain_matches(domain, blocked):
            decision.block = Block(422, "recipient_blocked", f"Recipient domain '{domain}' is blocked by policy.",
                                   "blocked_domain", {"recipient": email})
            return decision
        if allowed and not _domain_matches(domain, allowed):
            decision.block = Block(422, "recipient_blocked", f"Recipient domain '{domain}' is not allowed by policy.",
                                   "domain_not_allowed", {"recipient": email})
            return decision

    limits = policy.get("limits") or {}
    for key, window in (("emails_per_minute", timedelta(minutes=1)), ("emails_per_hour", timedelta(hours=1)),
                        ("emails_per_day", timedelta(days=1))):
        cap = limits.get(key)
        if cap is not None and await _count_outbound(session, inbox.id, window) >= cap:
            decision.block = Block(429, "rate_limited", f"Inbox send limit reached ({key}={cap}).", key,
                                   {"limit": key, "value": cap})
            return decision
    loop_cap = limits.get("per_thread_per_hour")
    if thread_id and loop_cap is not None and await _count_outbound(session, inbox.id, timedelta(hours=1),
                                                                     thread_id) >= loop_cap:
        decision.block = Block(429, "rate_limited", "Too many messages in one thread; possible automation loop.",
                               "possible_automation_loop", {"thread_id": thread_id, "value": loop_cap})
        return decision

    ap = policy.get("attachments") or {}
    max_bytes = int(ap.get("max_size_mb", 20)) * 1024 * 1024
    for a in attachments:
        ext = a.filename.rsplit(".", 1)[-1].lower() if "." in a.filename else ""
        if not ap.get("allow_executables", False) and ext in BLOCKED_EXTENSIONS:
            decision.block = Block(422, "attachment_blocked", f"Attachment '{a.filename}' type is not allowed.",
                                   "executable_attachment", {"filename": a.filename})
            return decision
        if a.size_bytes > max_bytes:
            decision.block = Block(413, "attachment_blocked", f"Attachment '{a.filename}' exceeds policy size limit.",
                                   "attachment_too_large",
                                   {"filename": a.filename, "max_size_mb": ap.get("max_size_mb")})
            return decision

    approval = policy.get("approval") or {}
    if approval.get("external_domain"):
        own = await _org_domains(session, organization_id)
        for email in emails:
            if split_address(email)[1] not in own:
                decision.approval_reasons.append("external_domain")
                break
    if approval.get("new_recipient"):
        for email in emails:
            if not await _is_known_recipient(session, organization_id, email):
                decision.approval_reasons.append("new_recipient")
                break
    return decision
