from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentbox.domain.addresses import is_valid_email, normalize_email


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailAddress(Model):
    email: str
    name: str | None = None

    @field_validator("email")
    @classmethod
    def _valid(cls, v: str) -> str:
        if not is_valid_email(v):
            raise ValueError(f"invalid email address: {v!r}")
        return normalize_email(v)


def coerce_addresses(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, str | dict):
        value = [value]
    return [{"email": v} if isinstance(v, str) else v for v in value]


class MeOut(BaseModel):
    organization_id: str
    api_key_id: str
    scopes: list[str] = Field(default_factory=list)
    environment: str


class InboxCreate(Model):
    username: str | None = None
    domain: str | None = None
    display_name: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl: str | None = None


class InboxOut(BaseModel):
    id: str
    email: str
    username: str
    display_name: str | None
    status: str
    provider_mode: str
    metadata: dict[str, Any]
    expires_at: str | None
    created_at: str
    updated_at: str


class AttachmentUploadCreate(Model):
    filename: str = Field(min_length=1, max_length=512)
    content_type: str = Field(default="application/octet-stream", max_length=255)
    size_bytes: int = Field(gt=0)


class WebhookCreate(Model):
    url: str = Field(max_length=2048)
    event_types: list[str] = Field(default_factory=lambda: ["*"])
    inbox_id: str | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("url must start with http:// or https://")
        return v


class WebhookUpdate(Model):
    url: str | None = Field(default=None, max_length=2048)
    event_types: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    description: str | None = Field(default=None, max_length=500)


class MessageSend(Model):
    to: list[EmailAddress] = Field(min_length=1)
    cc: list[EmailAddress] = Field(default_factory=list)
    bcc: list[EmailAddress] = Field(default_factory=list)
    reply_to: list[EmailAddress] = Field(default_factory=list)
    subject: str = Field(default="", max_length=998)
    text: str | None = None
    html: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=100)
    headers: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("to", "cc", "bcc", "reply_to", mode="before")
    @classmethod
    def _coerce(cls, v):
        return coerce_addresses(v)


class MessageReply(Model):
    text: str | None = None
    html: str | None = None
    reply_all: bool = False
    to: list[EmailAddress] = Field(default_factory=list)
    cc: list[EmailAddress] = Field(default_factory=list)
    bcc: list[EmailAddress] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _coerce(cls, v):
        return coerce_addresses(v)


class MessageForward(Model):
    to: list[EmailAddress] = Field(min_length=1)
    cc: list[EmailAddress] = Field(default_factory=list)
    bcc: list[EmailAddress] = Field(default_factory=list)
    text: str | None = None
    html: str | None = None
    include_attachments: bool = True

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _coerce(cls, v):
        return coerce_addresses(v)


class DomainCreate(Model):
    domain: str = Field(min_length=3, max_length=253)

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        import re

        v = v.strip().lower().rstrip(".")
        if not re.match(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", v):
            raise ValueError("invalid domain name")
        return v


class SuppressionCreate(Model):
    email: str
    reason: str = Field(default="manual", pattern="^(hard_bounce|complaint|manual|policy|invalid|abuse)$")
    note: str | None = Field(default=None, max_length=500)
    expires_at: str | None = None

    @field_validator("email")
    @classmethod
    def _valid(cls, v: str) -> str:
        if not is_valid_email(v):
            raise ValueError("invalid email address")
        return normalize_email(v)


class ApprovalReject(Model):
    reason: str | None = Field(default=None, max_length=1000)


class ProviderAccountCreate(Model):
    provider: str = Field(pattern="^(smtp_relay|sendgrid|unisender_go)$")
    name: str = Field(min_length=1, max_length=100)
    config: dict[str, Any]


class RoutingRuleCreate(Model):
    priority: int = Field(default=100, ge=0, le=100000)
    match: dict[str, Any] = Field(default_factory=dict)
    provider_account_id: str
    description: str | None = Field(default=None, max_length=500)

    @field_validator("match")
    @classmethod
    def _match(cls, v: dict) -> dict:
        allowed = {"recipient_domain_suffix", "inbox_id"}
        bad = set(v) - allowed
        if bad:
            raise ValueError(f"unsupported match keys: {sorted(bad)}")
        return v
