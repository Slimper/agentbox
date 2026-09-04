import base64

import httpx

from agentbox.mime.build import OutboundMessage
from agentbox.providers.base import Envelope, NormalizedEvent, PermanentError, SendResult, TemporaryError

EVENT_MAP = {"sent": "provider_accepted", "delivered": "delivered", "soft_bounced": "deferred",
             "hard_bounced": "bounced", "spam": "complained"}


class UnisenderGoProvider:
    name = "unisender_go"

    def __init__(self, api_key: str, http: httpx.AsyncClient, base_url: str = "https://go1.unisender.ru") -> None:
        self.api_key, self.http, self.base_url = api_key, http, base_url.rstrip("/")

    def build_payload(self, message: OutboundMessage, agentbox_message_id: str) -> dict:
        recipients = []
        for a in message.to + message.cc + message.bcc:
            r = {"email": a.email}
            if a.name:
                r["substitutions"] = {"to_name": a.name}
            recipients.append(r)
        headers = {name: value for name, value in message.headers}
        headers["Message-ID"] = message.message_id
        if message.in_reply_to:
            headers["In-Reply-To"] = message.in_reply_to
        if message.references:
            headers["References"] = " ".join(message.references)
        if message.cc:
            headers["CC"] = ", ".join(a.email for a in message.cc)
        body: dict = {}
        if message.html is not None:
            body["html"] = message.html
        if message.text is not None:
            body["plaintext"] = message.text
        msg: dict = {
            "recipients": recipients, "body": body, "subject": message.subject, "from_email": message.from_.email,
            "headers": headers, "global_metadata": {"agentbox_message_id": agentbox_message_id},
        }
        if message.from_.name:
            msg["from_name"] = message.from_.name
        if message.reply_to:
            msg["reply_to"] = message.reply_to[0].email
        regular = [a for a in message.attachments if not (a.disposition == "inline" and a.content_id)]
        inline = [a for a in message.attachments if a.disposition == "inline" and a.content_id]
        if regular:
            msg["attachments"] = [{"type": a.content_type, "name": a.filename,
                                   "content": base64.b64encode(a.content).decode("ascii")} for a in regular]
        if inline:
            msg["inline_attachments"] = [{"type": a.content_type, "name": a.content_id,
                                          "content": base64.b64encode(a.content).decode("ascii")} for a in inline]
        return {"message": msg}

    async def send(self, envelope: Envelope, message: OutboundMessage, raw: bytes) -> SendResult:
        agentbox_id = envelope.mail_from.split("@")[0].removeprefix("bounce+")
        payload = self.build_payload(message, f"msg_{agentbox_id.upper()}")
        try:
            resp = await self.http.post(f"{self.base_url}/ru/transactional/api/v1/email/send.json", json=payload,
                                        headers={"X-API-KEY": self.api_key}, timeout=30.0)
        except httpx.HTTPError as e:
            raise TemporaryError(f"{type(e).__name__}: {e}") from e
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and data.get("status") == "success":
            failed = data.get("failed_emails") or {}
            if failed and len(failed) >= len(payload["message"]["recipients"]):
                raise PermanentError(f"all recipients failed: {failed}")
            return SendResult(accepted=True, provider_message_id=data.get("job_id"), response="success",
                              refused={k: str(v) for k, v in failed.items()})
        detail = (data.get("message") or resp.text)[:500]
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TemporaryError(f"HTTP {resp.status_code}: {detail}")
        raise PermanentError(f"HTTP {resp.status_code}: {detail}")

    async def health(self) -> bool:
        try:
            resp = await self.http.post(f"{self.base_url}/ru/transactional/api/v1/domain/list.json", json={},
                                        headers={"X-API-KEY": self.api_key}, timeout=10.0)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    @staticmethod
    def parse_events(payload) -> list[NormalizedEvent]:
        out = []
        if not isinstance(payload, dict):
            return out
        for user in payload.get("events_by_user") or []:
            for ev in user.get("events") or []:
                if ev.get("event_name") != "transactional_email_status":
                    continue
                data = ev.get("event_data") or {}
                status = EVENT_MAP.get(data.get("status"))
                if not status:
                    continue
                info = data.get("delivery_info") or {}
                out.append(NormalizedEvent(
                    agentbox_message_id=(data.get("metadata") or {}).get("agentbox_message_id"), status=status,
                    provider_event_id=data.get("job_id"), reason_code=info.get("destination_response_code"),
                    reason=info.get("destination_response") or info.get("delivery_status"),
                    recipient=(data.get("email") or "").lower() or None, occurred_at=data.get("event_time"),
                ))
        return out
