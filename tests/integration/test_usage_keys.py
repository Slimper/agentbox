from datetime import timedelta

from sqlalchemy import select

from agentbox.db.models import Job, UsageDaily, utcnow
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.queue import ensure_periodic_jobs
from agentbox.jobs.worker import JobWorker


async def test_usage_rollup_and_api(client, org, runtime):
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "u"})).json()
    await client.post("/v1/inboxes", headers=org.headers, json={"ttl": "1h"})
    for i in range(3):
        await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                          json={"to": [f"v{i}@example.com"], "subject": "s", "text": "x"})
    await client.post("/v1/webhooks", headers=org.headers, json={"url": "http://127.0.0.1:9/hook"})
    live = (await client.get("/v1/usage/current", headers=org.headers)).json()
    assert live["active_inboxes"] == 2 and live["messages_sent"] == 3 and live["ephemeral_inboxes_created"] == 1
    async with runtime.db.session() as s:
        await ensure_periodic_jobs(s)
        await ensure_periodic_jobs(s)  # idempotent within the period
        await s.commit()
        jobs = (await s.scalars(select(Job).where(Job.kind == "usage_rollup"))).all()
        assert len(jobs) == 1
    await JobWorker(runtime, default_handlers(), concurrency=1, kinds=["usage_rollup"]).drain()
    async with runtime.db.session() as s:
        rows = (await s.scalars(select(UsageDaily).where(UsageDaily.organization_id == org.id)
                                .order_by(UsageDaily.day))).all()
        assert [r.messages_sent for r in rows] == [0, 3] and rows[-1].active_inboxes == 2
    r = await client.get("/v1/usage", headers=org.headers)
    body = r.json()
    assert body["totals"]["messages_sent"] == 3 and body["latest"]["active_inboxes"] == 2
    assert len(body["data"]) == 2
    since = (utcnow() - timedelta(days=1)).date().isoformat()
    r = await client.get("/v1/usage", headers=org.headers, params={"since": since, "until": since})
    assert len(r.json()["data"]) == 1 and r.json()["totals"]["messages_sent"] == 0


async def test_api_keys_lifecycle(client, org, make_org):
    r = await client.get("/v1/api-keys", headers=org.headers)
    assert len(r.json()["data"]) == 1 and "admin" in r.json()["scopes"]
    r = await client.post("/v1/api-keys", headers=org.headers,
                          json={"name": "reader", "scopes": ["inboxes:read"], "environment": "test"})
    assert r.status_code == 201 and r.json()["api_key"].startswith("ab_test_")
    reader_key = r.json()["api_key"]
    reader_headers = {"Authorization": f"Bearer {reader_key}"}
    assert (await client.get("/v1/inboxes", headers=reader_headers)).status_code == 200
    assert (await client.post("/v1/inboxes", headers=reader_headers, json={})).status_code == 403
    assert (await client.post("/v1/api-keys", headers=org.headers,
                              json={"name": "x", "scopes": ["nope"]})).status_code == 422
    own_id = (await client.get("/v1/me", headers=org.headers)).json()["api_key_id"]
    assert (await client.delete(f"/v1/api-keys/{own_id}", headers=org.headers)).status_code == 409
    assert (await client.delete(f"/v1/api-keys/{r.json()['id']}", headers=org.headers)).status_code == 204
    assert (await client.get("/v1/inboxes", headers=reader_headers)).status_code == 401
    other = await make_org("Other")
    assert (await client.delete(f"/v1/api-keys/{r.json()['id']}", headers=other.headers)).status_code == 404
    events = (await client.get("/v1/events", headers=org.headers, params={"type": "api_key.revoked"})).json()["data"]
    assert events and events[0]["data"]["name"] == "reader"
