from sqlalchemy import select

from agentbox.db.models import Domain, Event, Job
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker
from tests.unit.test_dns_check import FakeResolver


async def _drain(runtime):
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()


async def test_domain_lifecycle(client, org, runtime, make_org):
    r = await client.post("/v1/domains", headers=org.headers, json={"domain": "Agents.Company.RU"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "verification_pending" and d["domain"] == "agents.company.ru"
    token = [x for x in d["dns"] if x["purpose"] == "ownership"][0]["value"].split("=", 1)[1]
    # inbox on unverified domain is refused
    r = await client.post("/v1/inboxes", headers=org.headers, json={"username": "a", "domain": "agents.company.ru"})
    assert r.status_code == 422
    # someone else cannot claim it
    other = await make_org("Other")
    r = await client.post("/v1/domains", headers=other.headers, json={"domain": "agents.company.ru"})
    assert r.status_code == 409
    # DNS not set yet: stays pending, re-check scheduled
    runtime.dns = FakeResolver()
    await _drain(runtime)
    d = (await client.get(f"/v1/domains/{d['id']}", headers=org.headers)).json()
    assert d["status"] == "verification_pending" and d["check_results"]["ownership"] == "missing"
    async with runtime.db.session() as s:
        pending = (await s.scalars(select(Job).where(Job.kind == "domain_verify", Job.status == "pending"))).all()
        assert len(pending) == 1 and pending[0].payload["scheduled"] is True
    # customer sets DNS, requests verification
    runtime.dns = FakeResolver(txt={"_agentbox.agents.company.ru": [f"agentbox-verification={token}"]},
                               mx={"agents.company.ru": [(10, "mx1.agentbox.local")]})
    assert (await client.post(f"/v1/domains/{d['id']}/verify", headers=org.headers)).status_code == 202
    await _drain(runtime)
    d = (await client.get(f"/v1/domains/{d['id']}", headers=org.headers)).json()
    assert d["status"] == "active" and d["mx_status"] == "ok" and d["verified_at"]
    async with runtime.db.session() as s:
        assert await s.scalar(select(Event).where(Event.type == "domain.verified")) is not None
        pending = (await s.scalars(select(Job).where(Job.kind == "domain_verify", Job.status == "pending"))).all()
        assert len(pending) == 1
    # inbox on the verified custom domain
    r = await client.post("/v1/inboxes", headers=org.headers, json={"username": "proc", "domain": "agents.company.ru"})
    assert r.status_code == 201 and r.json()["email"] == "proc@agents.company.ru"
    inbox_id = r.json()["id"]
    # other org cannot use it
    r = await client.post("/v1/inboxes", headers=other.headers, json={"username": "x", "domain": "agents.company.ru"})
    assert r.status_code == 422
    # DNS breaks -> degraded
    runtime.dns = FakeResolver()
    await client.post(f"/v1/domains/{d['id']}/verify", headers=org.headers)
    await _drain(runtime)
    d = (await client.get(f"/v1/domains/{d['id']}", headers=org.headers)).json()
    assert d["status"] == "degraded"
    async with runtime.db.session() as s:
        assert await s.scalar(select(Event).where(Event.type == "domain.degraded")) is not None
    # cannot delete while inbox exists; then delete
    assert (await client.delete(f"/v1/domains/{d['id']}", headers=org.headers)).status_code == 409
    await client.delete(f"/v1/inboxes/{inbox_id}", headers=org.headers)
    assert (await client.delete(f"/v1/domains/{d['id']}", headers=org.headers)).status_code == 204
    assert (await client.get(f"/v1/domains/{d['id']}", headers=org.headers)).status_code == 404
    async with runtime.db.session() as s:
        row = await s.get(Domain, d["id"])
        assert row.deleted_at is not None
    assert (await client.get("/v1/domains", headers=org.headers)).json()["data"] == []
    assert (await client.post("/v1/domains", headers=org.headers, json={"domain": "not a domain"})).status_code == 422
