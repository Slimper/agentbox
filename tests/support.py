import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]

os.environ["AGENTBOX_DATABASE_URL"] = os.environ.get(
    "AGENTBOX_TEST_DATABASE_URL", "postgresql+asyncpg://agentbox:agentbox@localhost:5434/agentbox_test"
)
os.environ["AGENTBOX_S3_BUCKET"] = "agentbox-test"
os.environ["AGENTBOX_APP_SECRET_KEY"] = "integration-test-secret"
os.environ["AGENTBOX_MANAGED_DOMAIN"] = "agentbox.local"
os.environ["AGENTBOX_SMTP_BIND_PORT"] = "2526"

from agentbox.config import Settings  # noqa: E402
from agentbox.db.models import Base  # noqa: E402
from agentbox.db.seed import ensure_seed_data  # noqa: E402
from agentbox.db.session import Database  # noqa: E402

MAILPIT_URL = os.environ.get("MAILPIT_URL", "http://localhost:8025")


def pytest_collection_modifyitems(items):
    for item in items:
        path = str(item.fspath)
        if "/integration/" in path:
            item.add_marker(pytest.mark.integration)
        if "/e2e/" in path:
            item.add_marker(pytest.mark.e2e)


def _needs_services(request) -> bool:
    return bool(request.node.get_closest_marker("integration") or request.node.get_closest_marker("e2e"))


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture(scope="session")
def migrated():
    env = {**os.environ}
    subprocess.run([sys.executable, "-c", "from agentbox.db.migrate import upgrade; upgrade()"], cwd=ROOT, env=env, check=True)
    yield
    subprocess.run([sys.executable, "-c", "from agentbox.db.migrate import downgrade; downgrade()"], cwd=ROOT, env=env, check=True)


@pytest.fixture(scope="session")
async def db(settings, migrated):
    database = Database(settings.database_url)
    yield database
    await database.dispose()


@pytest.fixture(autouse=True)
async def clean_db(request, settings):
    if not _needs_services(request):
        yield
        return
    db = request.getfixturevalue("db")
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with db.session() as s:
        await s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await s.commit()
        await ensure_seed_data(s, settings)
    yield


import httpx  # noqa: E402

from agentbox.runtime import Runtime  # noqa: E402
from agentbox.storage.s3 import ObjectStorage  # noqa: E402


@pytest.fixture(scope="session")
async def runtime(settings, db):
    rt = Runtime(settings=settings, db=db, storage=ObjectStorage(settings), http=httpx.AsyncClient(timeout=10.0))
    await rt.storage.ensure_bucket()
    yield rt
    await rt.http.aclose()
from dataclasses import dataclass  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402

from agentbox.api.app import create_app  # noqa: E402
from agentbox.services.organizations import create_api_key, create_organization  # noqa: E402


@dataclass
class OrgFixture:
    id: str
    api_key: str
    headers: dict


@pytest.fixture
def app(runtime):
    return create_app(runtime)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def make_org(runtime):
    async def _make(name: str = "Acme", scopes: tuple[str, ...] = ("admin",)) -> OrgFixture:
        async with runtime.db.session() as s:
            org = await create_organization(s, name)
            _, plaintext = await create_api_key(s, org.id, scopes=scopes)
            await s.commit()
        return OrgFixture(id=org.id, api_key=plaintext, headers={"Authorization": f"Bearer {plaintext}"})

    return _make


@pytest.fixture
async def org(make_org) -> OrgFixture:
    return await make_org()
from aiohttp import web  # noqa: E402


class Listener:
    def __init__(self) -> None:
        self.received: list[dict] = []
        self.status = 200
        self.url = ""

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.received.append({"headers": dict(request.headers), "body": body, "json": json.loads(body)})
        return web.Response(status=self.status, text="ok")


@pytest.fixture
async def webhook_listener():
    listener = Listener()
    app = web.Application()
    app.router.add_post("/hook", listener.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    listener.url = f"http://127.0.0.1:{port}/hook"
    yield listener
    await runner.cleanup()


class Mailpit:
    def __init__(self, base: str) -> None:
        self.base = base

    async def clear(self) -> None:
        async with httpx.AsyncClient() as c:
            await c.delete(f"{self.base}/api/v1/messages")

    async def find(self, subject: str, timeout: float = 10.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient() as c:
            while True:
                r = await c.get(f"{self.base}/api/v1/messages", params={"limit": 200})
                for m in r.json().get("messages", []):
                    if m.get("Subject") == subject:
                        return (await c.get(f"{self.base}/api/v1/message/{m['ID']}")).json()
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError(f"no mailpit message with subject {subject!r}")
                await asyncio.sleep(0.3)

    async def headers(self, message_id: str) -> dict:
        async with httpx.AsyncClient() as c:
            return (await c.get(f"{self.base}/api/v1/message/{message_id}/headers")).json()


@pytest.fixture
async def mailpit():
    mp = Mailpit(MAILPIT_URL)
    await mp.clear()
    return mp
from agentbox.inbound.smtp_server import start_smtp_server  # noqa: E402


@pytest.fixture(scope="session")
async def smtp_edge(runtime):
    server = await start_smtp_server(runtime, host="127.0.0.1", port=runtime.settings.smtp_bind_port)
    yield ("127.0.0.1", runtime.settings.smtp_bind_port)
    server.close()
    await server.wait_closed()


# ---- live HTTP server (uvicorn in a thread) for SDK / TypeScript / MCP tests ----
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import uvicorn  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_api_url(settings, migrated):
    from agentbox.api.app import create_app

    port = _free_port()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("live API did not start")
    yield url
    server.should_exit = True
    thread.join(timeout=10)
