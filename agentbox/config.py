from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTBOX_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agentbox:agentbox@localhost:5434/agentbox"

    s3_endpoint: str = "http://localhost:9010"
    s3_public_endpoint: str | None = None
    s3_bucket: str = "agentbox"
    s3_access_key: str = "agentbox"
    s3_secret_key: str = "agentbox123"
    s3_region: str = "us-east-1"

    managed_domain: str = "agentbox.local"
    api_base_url: str = "http://localhost:8000"

    smtp_bind_host: str = "0.0.0.0"
    smtp_bind_port: int = 2525
    smtp_hostname: str = "mx.agentbox.local"
    smtp_tls_cert: str | None = None
    smtp_tls_key: str | None = None

    max_inbound_bytes: int = 30 * 1024 * 1024
    max_outbound_bytes: int = 25 * 1024 * 1024
    max_attachment_bytes: int = 20 * 1024 * 1024

    outbound_smtp_host: str = "localhost"
    outbound_smtp_port: int = 1025
    outbound_smtp_username: str | None = None
    outbound_smtp_password: str | None = None
    outbound_smtp_starttls: bool = False

    mx_hostnames: str = "mx1.agentbox.local,mx2.agentbox.local"
    spf_include: str = "spf.agentbox.local"
    dmarc_rua: str | None = None
    dkim_selector: str | None = None
    dkim_public_key: str | None = None
    dns_nameservers: str | None = None
    dns_timeout: float = 5.0
    domain_recheck_pending_seconds: int = 600
    domain_recheck_active_seconds: int = 21600

    api_rate_limit_per_minute: int = 600
    sendgrid_api_base: str = "https://api.sendgrid.com"
    unisender_api_base: str = "https://go1.unisender.ru"

    # edition: "oss" (self-host, API-key login only) or "cloud" (served by the agentbox-ee extension)
    edition: str = "oss"
    public_base_url: str = "http://localhost:8000"
    support_email: str = "sales@agentbox.ru"
    github_url: str = "https://github.com/Slimper/agentbox"
    app_secret_key: str = "CHANGE-ME-generate-with-agentbox-keygen"
    idempotency_ttl_seconds: int = 86400
    worker_concurrency: int = 4
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Settings of the most specific installed edition (extensions may subclass Settings)."""
    from agentbox.extensions import registry

    return registry().settings_class()()
