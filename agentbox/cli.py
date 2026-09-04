import asyncio
import secrets
from pathlib import Path

import typer

from agentbox.config import get_settings

ROOT = Path(__file__).resolve().parents[1]

app = typer.Typer(help="AgentBox — programmable email identities for AI agents", no_args_is_help=True)
org_app = typer.Typer(help="Organizations")
app.add_typer(org_app, name="org")


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Run the HTTP API."""
    import uvicorn

    uvicorn.run("agentbox.api.app:app_factory", host=host, port=port, reload=reload, factory=True)


@app.command()
def migrate() -> None:
    """Apply database migrations and seed shared data."""
    from agentbox.db.migrate import upgrade
    from agentbox.db.seed import ensure_seed_data
    from agentbox.runtime import Runtime

    upgrade("heads")

    async def _seed() -> None:
        rt = Runtime.create()
        async with rt.db.session() as session:
            await ensure_seed_data(session, rt.settings)
        await rt.storage.ensure_bucket()
        await rt.close()

    asyncio.run(_seed())
    typer.echo("migrated")


@app.command()
def keygen() -> None:
    """Print a random value for AGENTBOX_APP_SECRET_KEY."""
    typer.echo(secrets.token_urlsafe(48))


@org_app.command("create")
def org_create(name: str, key_name: str = "default", environment: str = "live") -> None:
    """Create an organization and print an admin API key (shown once)."""
    from agentbox.runtime import Runtime
    from agentbox.services.organizations import create_api_key, create_organization

    async def _run() -> None:
        rt = Runtime.create()
        async with rt.db.session() as session:
            org = await create_organization(session, name)
            _, plaintext = await create_api_key(session, org.id, name=key_name, environment=environment)
            await session.commit()
        await rt.close()
        typer.echo(f"organization_id={org.id}")
        typer.echo(f"api_key={plaintext}")

    asyncio.run(_run())


@app.command()
def worker(kinds: str = "", concurrency: int = 0) -> None:
    """Run the job worker (all kinds by default; --kinds outbound_send,webhook_deliver to restrict)."""
    import signal

    from agentbox.jobs.handlers import default_handlers
    from agentbox.jobs.worker import JobWorker
    from agentbox.logging import configure_logging
    from agentbox.runtime import Runtime

    async def _run() -> None:
        rt = Runtime.create()
        configure_logging(rt.settings.log_level)
        await rt.storage.ensure_bucket()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        w = JobWorker(rt, default_handlers(), kinds=[k for k in kinds.split(",") if k] or None,
                      concurrency=concurrency or rt.settings.worker_concurrency)
        typer.echo(f"worker started (kinds={w.kinds}, concurrency={w.concurrency})")
        await w.run(stop)
        await rt.close()

    asyncio.run(_run())


@app.command()
def smtp(host: str | None = None, port: int | None = None) -> None:
    """Run the inbound SMTP edge."""
    import signal

    from agentbox.inbound.smtp_server import start_smtp_server
    from agentbox.logging import configure_logging
    from agentbox.runtime import Runtime

    async def _run() -> None:
        rt = Runtime.create()
        configure_logging(rt.settings.log_level)
        await rt.storage.ensure_bucket()
        server = await start_smtp_server(rt, host=host, port=port)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        server.close()
        await server.wait_closed()
        await rt.close()

    asyncio.run(_run())


@org_app.command("set-plan")
def org_set_plan(organization_id: str, plan: str) -> None:
    """Assign a plan (free | payg | enterprise) — enterprise contracts are set here by hand."""
    from sqlalchemy import select

    from agentbox.db.models import Organization
    from agentbox.runtime import Runtime

    if plan not in ("free", "payg", "enterprise"):
        raise typer.BadParameter("plan must be free, payg or enterprise")

    async def _run() -> None:
        rt = Runtime.create()
        async with rt.db.session() as session:
            org = await session.scalar(select(Organization).where(Organization.id == organization_id))
            if org is None:
                raise typer.BadParameter(f"organization {organization_id} not found")
            org.plan = plan
            org.billing_status = "ok"
            await session.commit()
            typer.echo(f"{org.name}: plan={plan}")
        await rt.close()

    asyncio.run(_run())


@org_app.command("list")
def org_list() -> None:
    """List organizations with plan and status."""
    from sqlalchemy import select

    from agentbox.db.models import Organization
    from agentbox.runtime import Runtime

    async def _run() -> None:
        rt = Runtime.create()
        async with rt.db.session() as session:
            for o in await session.scalars(select(Organization).order_by(Organization.created_at)):
                typer.echo(f"{o.id}  plan={o.plan:<10} billing={o.billing_status:<8} {o.name}")
        await rt.close()

    asyncio.run(_run())


@app.command()
def worker_once() -> None:
    """Process pending jobs once and exit (cron-friendly)."""
    from agentbox.jobs.handlers import default_handlers
    from agentbox.jobs.worker import JobWorker
    from agentbox.runtime import Runtime

    async def _run() -> None:
        rt = Runtime.create()
        n = await JobWorker(rt, default_handlers(), concurrency=1).drain(max_jobs=1000)
        await rt.close()
        typer.echo(f"processed {n} jobs")

    asyncio.run(_run())


from agentbox.extensions import registry  # noqa: E402

for _name, _sub in registry().cli():
    app.add_typer(_sub, name=_name)


@app.callback()
def _main() -> None:
    get_settings()
