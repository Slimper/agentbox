import base64

import httpx

from agentbox.mime.build import OutboundMessage
from agentbox.providers.base import Envelope, NormalizedEvent, PermanentError, SendResult, TemporaryError

EVENT_MAP = {"processed": "provider_accepted", "delivered": "delivered", "deferred": "deferred", "bounce": "bounced",
             "dropped": "rejected", "spamreport": "complained"}


def _addr(a) -> dict:
    d = {"email": a.email}
    if a.name:
        d["name"] = a.name
    return d


class SendGridProvider:
    name = "sendgrid"

    def __init__(self, api_key: str, http: httpx.AsyncClient, base_url: str = "https://api.sendgrid.com") -> None:
        self.api_key, self.http, self.base_url = api_key, http, base_url.rstrip("/")

    def build_payload(self, message: OutboundMessage, agentbox_message_id: str) -> dict:
        personalization: dict = {"to": [_addr(a) for a in message.to]}
        if message.cc:
            personalization["cc"] = [_addr(a) for a in message.cc]
        if message.bcc:
            personalization["bcc"] = [_addr(a) for a in message.bcc]
        content = []
        if message.text is not None:
            content.append({"type": "text/plain", "value": message.text})
        if message.html is not None:
            content.append({"type": "text/html", "value": message.html})
        headers = {name: value for name, value in message.headers}
        headers["Message-ID"] = message.message_id
        if message.in_reply_to:
            headers["In-Reply-To"] = message.in_reply_to
        if message.references:
            headers["References"] = " ".join(message.references)
        payload = {
            "personalizations": [personalization], "from": _addr(message.from_), "subject": message.subject,
            "content": content, "headers": headers, "custom_args": {"agentbox_message_id": agentbox_message_id},
        }
        if message.reply_to:
            payload["reply_to"] = _addr(message.reply_to[0])
        if message.attachments:
            payload["attachments"] = [
                {"content": base64.b64encode(a.content).decode("ascii"), "type": a.content_type, "filename": a.filename,
                 "disposition": a.disposition, **({"content_id": a.content_id} if a.content_id else {})}
                for a in message.attachments
            ]
        return payload

    async def send(self, envelope: Envelope, message: OutboundMessage, raw: bytes) -> SendResult:
        agentbox_id = envelope.mail_from.split("@")[0].removeprefix("bounce+")
        payload = self.build_payload(message, f"msg_{agentbox_id.upper()}")
        try:
            resp = await self.http.post(f"{self.base_url}/v3/mail/send", json=payload,
                                        headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30.0)
        except httpx.HTTPError as e:
            raise TemporaryError(f"{type(e).__name__}: {e}") from e
        if resp.status_code in (200, 202):
            return SendResult(accepted=True, provider_message_id=resp.headers.get("X-Message-Id"),
                              response=f"HTTP {resp.status_code}")
        body = resp.text[:500]
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TemporaryError(f"HTTP {resp.status_code}: {body}")
        raise PermanentError(f"HTTP {resp.status_code}: {body}")

    async def health(self) -> bool:
        try:
            resp = await self.http.get(f"{self.base_url}/v3/user/credits",
                                       headers={"Authorization": f"Bearer {self.api_key}"}, timeout=10.0)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    @staticmethod
    def parse_events(payload) -> list[NormalizedEvent]:
        out = []
        for ev in payload if isinstance(payload, list) else []:
            status = EVENT_MAP.get(ev.get("event"))
            if not status:
                continue
            out.append(NormalizedEvent(
                agentbox_message_id=ev.get("agentbox_message_id"), status=status,
                provider_event_id=ev.get("sg_event_id"), reason_code=str(ev.get("status") or "") or None,
                reason=ev.get("reason") or ev.get("response"), recipient=(ev.get("email") or "").lower() or None,
                occurred_at=str(ev.get("timestamp")) if ev.get("timestamp") else None,
            ))
        return out


def verify_sendgrid_signature(public_key_b64: str, signature_b64: str, timestamp: str, body: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        key = load_der_public_key(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), timestamp.encode() + body, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
