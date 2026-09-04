from sqlalchemy import select, text

from agentbox.db.models import Domain, ProviderAccount


async def test_schema_and_seed(db, settings):
    async with db.session() as s:
        names = (await s.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))).scalars().all()
        for t in ["organizations", "api_keys", "domains", "inboxes", "threads", "messages", "attachments",
                  "events", "webhooks", "webhook_deliveries", "delivery_attempts", "provider_accounts",
                  "inbound_ingests", "idempotency_keys", "jobs"]:
            assert t in names
        dom = await s.scalar(select(Domain).where(Domain.domain == settings.managed_domain))
        assert dom is not None and dom.status == "active" and dom.organization_id is None
        pa = await s.scalar(select(ProviderAccount).where(ProviderAccount.organization_id.is_(None)))
        assert pa is not None and pa.provider == "smtp_relay"
