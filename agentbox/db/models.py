from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


def _ts(**kw):
    return mapped_column(DateTime(timezone=True), **kw)


class Timestamped:
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow, nullable=False)


class Organization(Timestamped, Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="free", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    billing_email: Mapped[str | None] = mapped_column(String(320))
    billing_status: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)
    payment_method_id: Mapped[str | None] = mapped_column(String(100))
    payment_method_title: Mapped[str | None] = mapped_column(String(100))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    audit_retention_days: Mapped[int | None] = mapped_column(Integer)
    message_retention_days: Mapped[int | None] = mapped_column(Integer)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    environment: Mapped[str] = mapped_column(String(10), default="live", nullable=False)
    last_used_at: Mapped[datetime | None] = _ts()
    revoked_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)


class Domain(Timestamped, Base):
    __tablename__ = "domains"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="verification_pending", nullable=False)
    inbound_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    outbound_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    spf_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    dkim_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    dmarc_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    mx_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    verification_token: Mapped[str | None] = mapped_column(String(100))
    verified_at: Mapped[datetime | None] = _ts()
    last_checked_at: Mapped[datetime | None] = _ts()
    next_check_at: Mapped[datetime | None] = _ts()
    check_results: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = _ts()


class Inbox(Timestamped, Base):
    __tablename__ = "inboxes"
    __table_args__ = (
        Index("ix_inboxes_address_active", "address", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_inboxes_org_status", "organization_id", "status"),
        Index("ix_inboxes_expires_active", "expires_at", postgresql_where=text("status = 'active'")),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    provider_mode: Mapped[str] = mapped_column(String(20), default="managed", nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    expires_at: Mapped[datetime | None] = _ts()
    deleted_at: Mapped[datetime | None] = _ts()


class Thread(Timestamped, Base):
    __tablename__ = "threads"
    __table_args__ = (Index("ix_threads_inbox_last", "inbox_id", "last_message_at"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    inbox_id: Mapped[str] = mapped_column(ForeignKey("inboxes.id"), nullable=False)
    subject: Mapped[str] = mapped_column(Text, default="", nullable=False)
    subject_normalized: Mapped[str] = mapped_column(Text, default="", nullable=False, index=True)
    participants: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    last_message_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)


class Message(Timestamped, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_inbox_created", "inbox_id", "created_at"),
        Index("ix_messages_thread_created", "thread_id", "created_at"),
        UniqueConstraint("organization_id", "inbox_id", "internet_message_id", name="uq_messages_org_inbox_mid"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    inbox_id: Mapped[str] = mapped_column(ForeignKey("inboxes.id"), nullable=False)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    from_address: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    to_addresses: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    cc_addresses: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    bcc_addresses: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    reply_to_addresses: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    subject: Mapped[str] = mapped_column(Text, default="", nullable=False)
    text_body: Mapped[str | None] = mapped_column(Text)
    html_body: Mapped[str | None] = mapped_column(Text)
    internet_message_id: Mapped[str] = mapped_column(String(998), nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    references: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    headers: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    raw_storage_key: Mapped[str | None] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    sent_at: Mapped[datetime | None] = _ts()
    received_at: Mapped[datetime | None] = _ts()


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachments_status_expires", "status", "expires_at"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    disposition: Mapped[str] = mapped_column(String(20), default="attachment", nullable=False)
    content_id: Mapped[str | None] = mapped_column(String(255))
    scan_status: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    expires_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_org_created", "organization_id", "created_at"),
        Index("ix_events_resource_created", "resource_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(40), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)


class Webhook(Timestamped, Base):
    __tablename__ = "webhooks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    inbox_id: Mapped[str | None] = mapped_column(ForeignKey("inboxes.id"))
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    event_types: Mapped[list] = mapped_column(JSONB, default=lambda: ["*"], nullable=False)
    deleted_at: Mapped[datetime | None] = _ts()


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("webhook_id", "event_id", "attempt_number", name="uq_webhook_delivery_attempt"),
        Index("ix_webhook_deliveries_webhook_created", "webhook_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhooks.id"), nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_excerpt: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_account_id: Mapped[str | None] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = _ts()


class ProviderAccount(Timestamped, Base):
    __tablename__ = "provider_accounts"
    __table_args__ = (UniqueConstraint("webhook_token", name="uq_provider_accounts_webhook_token"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    webhook_token: Mapped[str | None] = mapped_column(String(64))


class RoutingRule(Timestamped, Base):
    __tablename__ = "routing_rules"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    match: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))


class Policy(Timestamped, Base):
    __tablename__ = "policies"
    __table_args__ = (
        Index("ix_policies_org_level", "organization_id", unique=True, postgresql_where=text("inbox_id IS NULL")),
        Index("ix_policies_inbox", "organization_id", "inbox_id", unique=True,
              postgresql_where=text("inbox_id IS NOT NULL")),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    inbox_id: Mapped[str | None] = mapped_column(ForeignKey("inboxes.id"))
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("organization_id", "email", name="uq_suppressions_org_email"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = _ts()


class InboundIngest(Base):
    __tablename__ = "inbound_ingests"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    inbox_id: Mapped[str | None] = mapped_column(ForeignKey("inboxes.id"))
    bounce_message_id: Mapped[str | None] = mapped_column(String(40))
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mail_from: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    rcpt_to: Mapped[str] = mapped_column(String(320), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = _ts()


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    organization_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = _ts(nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_at", "status", "run_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    run_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    locked_at: Mapped[datetime | None] = _ts()
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow, nullable=False)


class UsageDaily(Base):
    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("organization_id", "day", name="uq_usage_daily_org_day"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    active_inboxes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ephemeral_inboxes_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attachment_bytes_stored: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    webhook_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    custom_domains: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    computed_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow, nullable=False)
