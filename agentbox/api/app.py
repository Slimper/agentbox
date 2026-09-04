from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import text
from starlette.datastructures import MutableHeaders
from ulid import ULID

from agentbox import __version__
from agentbox.api.errors import register_error_handlers
from agentbox.api.routers import (
    analytics,
    api_keys,
    approvals,
    attachments,
    domains,
    events,
    inboxes,
    me,
    messages,
    policies,
    provider_events,
    providers,
    suppressions,
    threads,
    usage,
    webhooks,
)
from agentbox.config import get_settings
from agentbox.dashboard.router import register_dashboard_handlers
from agentbox.dashboard.router import router as dashboard_router
from agentbox.db.seed import ensure_seed_data
from agentbox.extensions import registry
from agentbox.logging import configure_logging
from agentbox.runtime import Runtime


class RequestIdMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = f"req_{ULID()}"
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("AgentBox-Request-Id", request_id)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    from agentbox.dashboard.router import _TEMPLATE_DIRS, templates

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or Runtime.create()
        configure_logging(rt.settings.log_level)
        app.state.runtime = rt
        templates.env.globals.update(github_url=rt.settings.github_url, support_email=rt.settings.support_email)
        async with rt.db.session() as session:
            await ensure_seed_data(session, rt.settings)
        await rt.storage.ensure_bucket()
        yield
        if runtime is None:
            await rt.close()

    app = FastAPI(title="AgentBox", version=__version__, lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    register_dashboard_handlers(app)
    app.include_router(me.router)
    app.include_router(inboxes.router)
    app.include_router(attachments.router)
    app.include_router(webhooks.router)
    app.include_router(messages.router)
    app.include_router(threads.router)
    app.include_router(events.router)
    app.include_router(domains.router)
    for r in (policies, suppressions, approvals, providers, provider_events, analytics, usage, api_keys):
        app.include_router(r.router)

    app.include_router(dashboard_router)
    settings = runtime.settings if runtime is not None else get_settings()
    for ext_router in registry().routers(settings):
        app.include_router(ext_router)
    template_dirs = [*_TEMPLATE_DIRS, *registry().template_dirs(settings)]
    templates.env.loader = ChoiceLoader([FileSystemLoader(d) for d in template_dirs])
    static_dir = Path(__file__).resolve().parents[1] / "dashboard" / "static"
    app.mount("/dashboard/static", StaticFiles(directory=str(static_dir)), name="dashboard-static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request):
        async with request.app.state.runtime.db.session() as session:
            await session.execute(text("SELECT 1"))
        return {"ok": True}

    return app


def app_factory() -> FastAPI:
    return create_app()
