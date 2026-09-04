from datetime import timedelta

from sqlalchemy import select

from agentbox.db.models import Event, Inbox, Job, utcnow
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker


async def test_create_and_get_inbox(client, org):
    r = await client.post("/v1/inboxes", headers=org.headers,
                          json={"username": "Procurement-Agent", "display_name": "Proc",
                                "metadata": {"agent_id": "a1"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "procurement-agent@agentbox.local"
    assert body["status"] == "active" and body["metadata"] == {"agent_id": "a1"}
    r = await client.get(f"/v1/inboxes/{body['id']}", headers=org.headers)
    assert r.status_code == 200 and r.json()["id"] == body["id"]


async def test_generated_username_and_reserved(client, org):
    r = await client.post("/v1/inboxes", headers=org.headers, json={})
    assert r.status_code == 201 and "@agentbox.local" in r.json()["email"]
    r = await client.post("/v1/inboxes", headers=org.headers, json={"username": "postmaster"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


async def test_duplicate_address_conflict_and_reuse_after_delete(client, org, make_org):
    r1 = await client.post("/v1/inboxes", headers=org.headers, json={"username": "dup"})
    assert r1.status_code == 201
    other = await make_org("Other")
    r2 = await client.post("/v1/inboxes", headers=other.headers, json={"username": "dup"})
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "conflict"
    assert (await client.delete(f"/v1/inboxes/{r1.json()['id']}", headers=org.headers)).status_code == 204
    r3 = await client.post("/v1/inboxes", headers=other.headers, json={"username": "dup"})
    assert r3.status_code == 201


async def test_tenant_isolation_and_scopes(client, org, make_org):
    r = await client.post("/v1/inboxes", headers=org.headers, json={"username": "mine"})
    inbox_id = r.json()["id"]
    other = await make_org("Other")
    assert (await client.get(f"/v1/inboxes/{inbox_id}", headers=other.headers)).status_code == 404
    reader = await make_org("Reader", scopes=("inboxes:read",))
    r = await client.post("/v1/inboxes", headers=reader.headers, json={"username": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


async def test_list_filters_and_pagination(client, org):
    for i in range(3):
        await client.post("/v1/inboxes", headers=org.headers,
                          json={"username": f"agent-{i}", "metadata": {"team": "a" if i < 2 else "b"}})
    r = await client.get("/v1/inboxes", headers=org.headers, params={"limit": 2})
    assert r.status_code == 200 and len(r.json()["data"]) == 2 and r.json()["next_cursor"]
    r2 = await client.get("/v1/inboxes", headers=org.headers, params={"limit": 2, "cursor": r.json()["next_cursor"]})
    assert len(r2.json()["data"]) == 1 and r2.json()["next_cursor"] is None
    r3 = await client.get("/v1/inboxes", headers=org.headers, params={"metadata.team": "a"})
    assert len(r3.json()["data"]) == 2
    r4 = await client.get("/v1/inboxes", headers=org.headers, params={"domain": "agentbox.local", "status": "active"})
    assert len(r4.json()["data"]) == 3


async def test_disable_enable_delete_emit_events(client, org, runtime):
    inbox_id = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "s"})).json()["id"]
    r = await client.post(f"/v1/inboxes/{inbox_id}/disable", headers=org.headers)
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    r = await client.post(f"/v1/inboxes/{inbox_id}/enable", headers=org.headers)
    assert r.json()["status"] == "active"
    async with runtime.db.session() as s:
        types = (await s.scalars(select(Event.type).where(Event.resource_id == inbox_id).order_by(Event.id))).all()
        assert types == ["inbox.created", "inbox.disabled", "inbox.enabled"]
        jobs = (await s.scalars(select(Job).where(Job.kind == "webhook_deliver"))).all()
        assert len(jobs) == 3


async def test_ttl_creates_expiry_job_and_expires(client, org, runtime):
    r = await client.post("/v1/inboxes", headers=org.headers, json={"ttl": "1h"})
    assert r.status_code == 201 and r.json()["expires_at"]
    inbox_id = r.json()["id"]
    assert (await client.post("/v1/inboxes", headers=org.headers, json={"ttl": "40d"})).status_code == 422
    async with runtime.db.session() as s:
        job = await s.scalar(select(Job).where(Job.kind == "inbox_expire"))
        assert job is not None and job.payload == {"inbox_id": inbox_id}
        assert job.run_at > utcnow() + timedelta(minutes=55)
        job.run_at = utcnow()
        inbox = await s.get(Inbox, inbox_id)
        inbox.expires_at = utcnow() - timedelta(seconds=1)
        await s.commit()
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    r = await client.get(f"/v1/inboxes/{inbox_id}", headers=org.headers)
    assert r.json()["status"] == "expired"
    assert (await client.post(f"/v1/inboxes/{inbox_id}/enable", headers=org.headers)).status_code == 409
