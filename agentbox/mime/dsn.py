from __future__ import annotations

import email
import email.policy
from dataclasses import dataclass


@dataclass
class DSNRecipient:
    recipient: str | None
    action: str | None
    status: str | None
    diagnostic: str | None

    def to_dict(self) -> dict:
        return {"recipient": self.recipient, "action": self.action, "status": self.status,
                "diagnostic": self.diagnostic}


def _field(block, name: str) -> str | None:
    value = block.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_dsn(raw: bytes) -> list[DSNRecipient]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    out: list[DSNRecipient] = []
    for part in msg.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        blocks = part.get_payload()
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            action = _field(block, "Action")
            final = _field(block, "Final-Recipient")
            if action is None and final is None:
                continue
            recipient = final.split(";", 1)[-1].strip().lower() if final else None
            out.append(DSNRecipient(recipient=recipient, action=action.lower() if action else None,
                                    status=_field(block, "Status"), diagnostic=_field(block, "Diagnostic-Code")))
    return out


def classify_dsn(recipients: list[DSNRecipient]) -> str:
    for r in recipients:
        if r.action == "failed" or (r.status or "").startswith("5"):
            return "bounced"
    for r in recipients:
        if r.action == "delayed" or (r.status or "").startswith("4"):
            return "deferred"
    return "unknown"
