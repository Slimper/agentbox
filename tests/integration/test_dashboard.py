import httpx
import pytest
from sqlalchemy import select

from agentbox.db.models import Message, Policy, WebhookDelivery


@pytest.fixture
async def dash(app, org):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as c:
        r = await c.post("/dashboard/login", data={"api_key": org.api_key})
        assert r.status_code == 303 and r.headers["location"] == "/dashboard"
        assert "ab_dash" in r.cookies
        yield c


async def test_login_required_and_bad_key(client):
    r = await client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"
    r = await client.post("/dashboard/login", data={"api_key": "ab_live_nope"})
    assert r.status_code == 200 and "Invalid API key" in r.text
    assert (await client.get("/dashboard/static/ds.css")).status_code == 200


async def test_all_pages_render(dash):
    for path in ["/dashboard", "/dashboard?view=operations", "/dashboard?view=feed&feed=policy", "/dashboard/inboxes",
                 "/dashboard/inboxes?create=ephemeral", "/dashboard/approvals", "/dashboard/domains",
                 "/dashboard/domains?add=1", "/dashboard/webhooks", "/dashboard/webhooks?create=1",
                 "/dashboard/policies", "/dashboard/usage", "/dashboard/api-keys?create=1", "/dashboard/audit",
                 "/dashboard/audit?filter=keys", "/dashboard/quickstart", "/dashboard/console?ep=3&lang=python",
                 "/dashboard/search?q=agent"]:
        r = await dash.get(path)
        assert r.status_code == 200, path
        assert "AgentBox" in r.text and "ds.css" in r.text
    r = await dash.get("/dashboard/audit.csv")
    assert r.status_code == 200 and r.text.startswith("time,type")


async def test_inbox_flow_send_reply_policy(dash, org, runtime):
    r = await dash.post("/dashboard/inboxes", data={"username": "dash-agent", "display_name": "Dash", "domain": "",
                                                    "metadata": '{"team": "ops"}', "ttl": ""})
    assert r.status_code == 303 and "toast=" in r.headers["location"]
    inbox_url = r.headers["location"].split("?")[0]
    r = await dash.get(inbox_url)
    assert r.status_code == 200 and "dash-agent@agentbox.local" in r.text and "team=ops" in r.text
    r = await dash.post(f"{inbox_url}/send", data={"to": "vendor@example.com", "subject": "From dashboard",
                                                  "text": "hi <b>there</b>"})
    assert r.status_code == 303
    r = await dash.get(r.headers["location"])
    assert r.status_code == 200 and "From dashboard" in r.text and "&lt;b&gt;there&lt;/b&gt;" in r.text
    assert "Send reply" in r.text
    async with runtime.db.session() as s:
        msg = await s.scalar(select(Message))
    r = await dash.post(f"{inbox_url}/reply", data={"message_id": msg.id, "text": "follow-up"})
    assert r.status_code == 303
    r = await dash.get(r.headers["location"])
    assert "follow-up" in r.text
    async with runtime.db.session() as s:
        reply = await s.scalar(select(Message).where(Message.in_reply_to.is_not(None)))
    assert reply.subject == "Re: From dashboard" and reply.thread_id == msg.thread_id
    r = await dash.get(f"{inbox_url}?tab=policies")
    assert r.status_code == 200 and "Limits" in r.text
    r = await dash.post(f"{inbox_url}/policy", data={"config": '{"limits": {"emails_per_day": 3}}'})
    assert r.status_code == 303
    assert '&#34;emails_per_day&#34;: 3' in (await dash.get(f"{inbox_url}?tab=policies")).text
    assert (await dash.get(f"{inbox_url}?tab=events")).status_code == 200
    assert "dash-agent" in (await dash.get(f"{inbox_url}?tab=metadata")).text
    r = await dash.post(f"{inbox_url}/status", data={"action": "disable"})
    assert r.status_code == 303
    assert "inbox.disabled" in (await dash.get("/dashboard/audit", params={"type": "inbox.disabled"})).text
    r = await dash.get("/dashboard?view=operations")
    assert "dash-agent@agentbox.local" in r.text  # most active inboxes
    async with runtime.db.session() as s:
        m = await s.scalar(select(Message))
        m.html_body = "<p>ok</p><script>alert(1)</script>"
        await s.commit()
    r = await dash.get(f"/dashboard/messages/{m.id}")
    assert r.status_code == 200 and "<script>" not in r.text and "<p>ok</p>" in r.text


async def test_policy_form_webhooks_keys_console(dash, org, runtime):
    form = {"scope": "org", "mode": "form", "send_enabled": "on", "receive_enabled": "on",
            "allowed_domains": "supplier.ru, Partner.RU", "blocked_domains": "", "emails_per_minute": "7",
            "emails_per_hour": "50", "emails_per_day": "100", "per_thread_per_hour": "9", "loop_protection": "on",
            "max_size_mb": "10", "approval_external_domain": "on"}
    r = await dash.post("/dashboard/policies", data=form)
    assert r.status_code == 303
    async with runtime.db.session() as s:
        row = await s.scalar(select(Policy).where(Policy.organization_id == org.id))
        assert row.config["limits"]["emails_per_minute"] == 7 and row.config["approval"]["external_domain"] is True
        assert row.config["recipient_policy"]["allowed_domains"] == ["partner.ru", "supplier.ru"]
    bad = {"scope": "org", "mode": "json", "config": '{"bogus": 1}'}
    assert (await dash.post("/dashboard/policies", data=bad)).status_code == 422
    # webhook: create shows secret once, test event queues a delivery, rotate shows a new secret
    r = await dash.post("/dashboard/webhooks", data={"url": "https://example.com/h", "event_types": "message.received"})
    assert r.status_code == 303 and "secret=whsec_" in r.headers["location"]
    hook_id = r.headers["location"].split("hook=")[1].split("&")[0]
    assert "shown again" in (await dash.get(r.headers["location"])).text
    r = await dash.post(f"/dashboard/webhooks/{hook_id}/action", data={"action": "test"})
    assert r.status_code == 303
    async with runtime.db.session() as s:
        assert await s.scalar(select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook_id)) is not None
    r = await dash.post(f"/dashboard/webhooks/{hook_id}/action", data={"action": "rotate"})
    assert "secret=whsec_" in r.headers["location"]
    # api keys
    r = await dash.post("/dashboard/api-keys", data={"name": "ops", "scopes": ["inboxes:read"], "environment": "test"})
    assert r.status_code == 303 and "new_key=ab_test_" in r.headers["location"]
    # console runs a real request against the API with the session key
    r = await dash.post("/dashboard/console/run", data={"ep": 0, "lang": "curl", "resource_id": "",
                                                        "body": '{"username": "console-agent"}'})
    assert r.status_code == 200 and "201" in r.text and "console-agent@agentbox.local" in r.text
    r = await dash.post("/dashboard/console/run", data={"ep": 0, "lang": "curl", "resource_id": "", "body": "{bad"})
    assert r.status_code == 422
    # theme toggle + logout
    r = await dash.post("/dashboard/theme", data={"next": "/dashboard/usage"})
    assert r.status_code == 303 and r.cookies.get("ab_theme") == "light"
    r = await dash.post("/dashboard/logout")
    assert r.status_code == 303


async def test_scope_enforced_in_dashboard(app, make_org):
    reader = await make_org("Reader", scopes=("inboxes:read", "messages:read"))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as c:
        await c.post("/dashboard/login", data={"api_key": reader.api_key})
        assert (await c.get("/dashboard/inboxes")).status_code == 200
        r = await c.post("/dashboard/inboxes", data={"username": "x"})
        assert r.status_code == 403 and "forbidden" in r.text
