import base64
import re

import aiosmtplib

from agentbox.mime.build import OutboundMessage
from agentbox.providers.base import Envelope, PermanentError, SendResult, TemporaryError

_QUEUE_ID = re.compile(r"queued as ([^\s]+)", re.IGNORECASE)


class SMTPRelayProvider:
    name = "smtp_relay"

    def __init__(self, host: str, port: int, username: str | None = None, password: str | None = None,
                 starttls: bool = False, timeout: float = 30.0, use_tls: bool = False,
                 oauth_token: str | None = None) -> None:
        self.host, self.port, self.username, self.password = host, int(port), username or None, password or None
        self.starttls, self.timeout, self.use_tls = bool(starttls), timeout, bool(use_tls)
        self.oauth_token = oauth_token or None  # SASL XOAUTH2 (Microsoft 365, Gmail) instead of a password

    async def _send_xoauth2(self, envelope: Envelope, raw: bytes) -> tuple[dict, str]:
        client = aiosmtplib.SMTP(hostname=self.host, port=self.port, use_tls=self.use_tls,
                                 start_tls=(True if self.starttls else False) if not self.use_tls else False,
                                 timeout=self.timeout)
        await client.connect()
        try:
            if client.is_ehlo_or_helo_needed:
                await client.ehlo()
            auth = base64.b64encode(f"user={self.username}\x01auth=Bearer {self.oauth_token}\x01\x01".encode()).decode()
            resp = await client.execute_command(b"AUTH", b"XOAUTH2", auth.encode())
            if resp.code == 334:  # server sent an error challenge; acknowledge to get the final status
                resp = await client.execute_command(b"")
            if resp.code != 235:
                raise aiosmtplib.SMTPAuthenticationError(resp.code, resp.message)
            return await client.sendmail(envelope.mail_from, envelope.rcpt_to, raw)
        finally:
            try:
                await client.quit()
            except Exception:  # noqa: BLE001
                pass

    async def send(self, envelope: Envelope, message: OutboundMessage, raw: bytes) -> SendResult:
        try:
            if self.oauth_token:
                errors, response = await self._send_xoauth2(envelope, raw)
            else:
                errors, response = await aiosmtplib.send(
                    raw, sender=envelope.mail_from, recipients=envelope.rcpt_to, hostname=self.host, port=self.port,
                    username=self.username, password=self.password,
                    start_tls=(True if self.starttls else False) if not self.use_tls else False, use_tls=self.use_tls,
                    timeout=self.timeout,
                )
        except aiosmtplib.SMTPRecipientsRefused as e:
            raise PermanentError(f"all recipients refused: {e}") from e
        except aiosmtplib.SMTPResponseException as e:
            if 400 <= e.code < 500:
                raise TemporaryError(f"{e.code} {e.message}") from e
            raise PermanentError(f"{e.code} {e.message}") from e
        except (aiosmtplib.SMTPException, OSError) as e:
            raise TemporaryError(f"{type(e).__name__}: {e}") from e
        refused = {addr: f"{err.code} {err.message}" for addr, err in (errors or {}).items()}
        match = _QUEUE_ID.search(response or "")
        return SendResult(accepted=True, provider_message_id=match.group(1) if match else None,
                          response=response or "", refused=refused)

    async def health(self) -> bool:
        try:
            client = aiosmtplib.SMTP(hostname=self.host, port=self.port, timeout=5)
            await client.connect()
            await client.quit()
            return True
        except Exception:  # noqa: BLE001
            return False
