"""Canonical delivery-status transitions shared by DSN processing and provider event webhooks."""

from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.db.models import Message
from agentbox.governance.suppressions import add_suppression
from agentbox.services.events import emit

STATUS_RANK = {"queued": 0, "provider_accepted": 1, "deferred": 2, "delivered": 3, "bounced": 4, "rejected": 4,
               "complained": 4, "failed": 4}
CANONICAL = frozenset(STATUS_RANK)


async def apply_delivery_status(
    session: AsyncSession, message: Message, new_status: str, *, source: str, reason_code: str | None = None,
    reason_message: str | None = None, recipient: str | None = None, extra: dict | None = None,
) -> bool:
    """Move a message forward in its lifecycle, emit the canonical event, suppress on hard bounce/complaint.

    Returns True when the status actually changed."""
    if new_status not in CANONICAL:
        raise ValueError(f"unknown canonical status {new_status}")
    changed = STATUS_RANK[new_status] > STATUS_RANK.get(message.status, 0)
    if changed:
        message.status = new_status
        if reason_code or reason_message:
            message.error_code, message.error_message = reason_code, (reason_message or None)
    payload = {"inbox_id": message.inbox_id, "thread_id": message.thread_id, "message_id": message.id,
               "source": source, "recipient": recipient, "reason_code": reason_code, "reason": reason_message,
               "status_changed": changed, **(extra or {})}
    await emit(session, organization_id=message.organization_id, resource_type="message", resource_id=message.id,
               type=f"message.{new_status}", payload=payload)
    hard = new_status == "bounced" and (reason_code or "").startswith("5")
    if hard or new_status == "complained":
        targets = [recipient] if recipient else [a["email"] for a in message.to_addresses]
        for email in targets:
            await add_suppression(session, organization_id=message.organization_id, email=email,
                                  reason="hard_bounce" if hard else "complaint", source=source,
                                  provider=message.provider, note=reason_message)
    return changed
