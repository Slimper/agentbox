from dataclasses import dataclass, field
from typing import Protocol

from agentbox.mime.build import OutboundMessage


@dataclass(frozen=True)
class Envelope:
    mail_from: str
    rcpt_to: list[str]
    message_id: str


@dataclass
class SendResult:
    accepted: bool
    provider_message_id: str | None = None
    response: str = ""
    refused: dict[str, str] = field(default_factory=dict)


class ProviderError(Exception):
    code = "provider_error"


class TemporaryError(ProviderError):
    code = "provider_temporary_failure"


class PermanentError(ProviderError):
    code = "provider_permanent_failure"


@dataclass
class NormalizedEvent:
    agentbox_message_id: str | None
    status: str
    provider_event_id: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    recipient: str | None = None
    occurred_at: str | None = None


class OutboundProvider(Protocol):
    name: str

    async def send(self, envelope: Envelope, message: OutboundMessage, raw: bytes) -> SendResult: ...

    async def health(self) -> bool: ...
