from datetime import timedelta

from sqlalchemy import select

from agentbox.db.models import Event, Job, WebhookDelivery, utcnow
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.queue import enqueue
from agentbox.jobs.worker import JobWorker
from agentbox.services.events import emit
from agentbox.webhooks.signing import verify_signature


async def _emit(runtime, org_id, type="inbox.created", inbox_id="ibx_x"):
    async with runtime.db.session() as s:
        ev = await emit(s, organization_id=org_id, resource_type="inbox", resource_id=inbox_id, type=type,
                        payload={"inbox_id": inbox_id, "hello": "world"})
        await s.commit()
        return ev.id


async def test_crud_and_secret_once(client, org):
    r = await client.post("/v1/webhooks", headers=org.headers, json={"url": "https://example.com/h"})
    assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
    wid = r.json()["id"]
    assert "secret" not in (await client.get(f"/v1/webhooks/{wid}", headers=org.headers)).json()
    r = await client.patch(f"/v1/webhooks/{wid}", headers=org.headers, json={"event_types": ["message.received"]})
    assert r.json()["event_types"] == ["message.received"]
    assert (await client.post("/v1/webhooks", headers=org.headers, json={"url": "ftp://x"})).status_code == 422
    assert (await client.delete(f"/v1/webhooks/{wid}", headers=org.headers)).status_code == 204
    assert (await client.get(f"/v1/webhooks/{wid}", headers=org.headers)).status_code == 404


async def test_delivery_signed_and_recorded(client, org, runtime, webhook_listener):
    r = await client.post("/v1/webhooks", headers=org.headers, json={"url": webhook_listener.url})
    secret, wid = r.json()["secret"], r.json()["id"]
    event_id = await _emit(runtime, org.id)
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    assert len(webhook_listener.received) == 1
    got = webhook_listener.received[0]
    assert got["headers"]["AgentBox-Event-Id"] == event_id
    assert verify_signature(secret, got["headers"]["AgentBox-Signature"], got["body"])
    assert got["json"]["type"] == "inbox.created" and got["json"]["data"]["hello"] == "world"
    r = await client.get(f"/v1/webhooks/{wid}/deliveries", headers=org.headers)
    [d] = r.json()["data"]
    assert d["status"] == "succeeded" and d["response_status"] == 200 and d["attempt_number"] == 1


async def test_filtering_by_type_and_inbox(client, org, runtime, webhook_listener):
    await client.post("/v1/webhooks", headers=org.headers,
                      json={"url": webhook_listener.url, "event_types": ["message.received"]})
    await _emit(runtime, org.id, type="inbox.created")
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    assert webhook_listener.received == []
    await _emit(runtime, org.id, type="message.received")
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    assert len(webhook_listener.received) == 1


async def test_failure_schedules_retry_and_manual_retry(client, org, runtime, webhook_listener):
    webhook_listener.status = 500
    r = await client.post("/v1/webhooks", headers=org.headers, json={"url": webhook_listener.url})
    wid = r.json()["id"]
    await _emit(runtime, org.id)
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    async with runtime.db.session() as s:
        rows = (await s.scalars(select(WebhookDelivery).order_by(WebhookDelivery.attempt_number))).all()
        assert [d.status for d in rows] == ["failed", "pending"]
        assert rows[0].error == "HTTP 500"
        job = await s.scalar(select(Job).where(Job.kind == "webhook_deliver", Job.status == "pending"))
        assert job.payload == {"delivery_id": rows[1].id}
        assert timedelta(seconds=8) < job.run_at - utcnow() <= timedelta(seconds=10)
    webhook_listener.status = 200
    r = await client.post(f"/v1/webhooks/{wid}/deliveries/{rows[0].id}/retry", headers=org.headers)
    assert r.status_code == 202 and r.json()["attempt_number"] == 3
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    assert len(webhook_listener.received) == 2


async def test_exhaustion_emits_webhook_failed(client, org, runtime, webhook_listener):
    webhook_listener.status = 503
    r = await client.post("/v1/webhooks", headers=org.headers, json={"url": webhook_listener.url})
    wid = r.json()["id"]
    event_id = await _emit(runtime, org.id)
    async with runtime.db.session() as s:
        d = WebhookDelivery(id="wdl_TESTEXHAUST00000000000000", organization_id=org.id, webhook_id=wid,
                            event_id=event_id, attempt_number=8, status="pending", scheduled_at=utcnow())
        s.add(d)
        await s.flush()
        await enqueue(s, "webhook_deliver", {"delivery_id": d.id})
        await s.commit()
    worker = JobWorker(runtime, default_handlers(), concurrency=1)
    await worker.drain()
    async with runtime.db.session() as s:
        d = await s.get(WebhookDelivery, "wdl_TESTEXHAUST00000000000000")
        assert d.status == "exhausted"
        failed = await s.scalar(select(Event).where(Event.type == "webhook.failed"))
        assert failed is not None and failed.payload["attempts"] == 8
