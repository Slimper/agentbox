import hashlib

import httpx
from sqlalchemy import select

from agentbox.db.models import DeliveryAttempt, Event, Job, Message, ProviderAccount
from agentbox.domain.ids import new_id
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker
from agentbox.security.crypto import encrypt_json


async def test_send_reaches_mailpit_with_headers_and_attachment(client, org, runtime, mailpit):
    inbox = (await client.post("/v1/inboxes", headers=org.headers,
                               json={"username": "rfq", "display_name": "RFQ Agent"})).json()
    data = b"%PDF-1.4 spec" * 10
    up = (await client.post("/v1/attachments/uploads", headers=org.headers,
                            json={"filename": "spec.pdf", "content_type": "application/pdf",
                                  "size_bytes": len(data)})).json()
    async with httpx.AsyncClient() as ext:
        await ext.put(up["upload_url"], content=data, headers={"Content-Type": "application/pdf"})
    r = await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                          json={"to": [{"email": "sales@supplier.ru", "name": "Sales"}], "cc": ["cc@supplier.ru"],
                                "bcc": ["hidden@supplier.ru"], "subject": "Запрос КП #1", "text": "Привет",
                                "html": "<p>Привет</p>", "attachment_ids": [up["attachment_id"]],
                                "headers": {"X-Agent-Id": "a1"}})
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    m = await mailpit.find("Запрос КП #1")
    assert m["From"]["Address"] == "rfq@agentbox.local" and m["From"]["Name"] == "RFQ Agent"
    assert sorted(a["Address"] for a in m["To"]) == ["sales@supplier.ru"]
    assert [a["Address"] for a in m["Cc"]] == ["cc@supplier.ru"]
    assert m["ReturnPath"].startswith("bounce+") and m["ReturnPath"].endswith("@agentbox.local")
    assert [a["FileName"] for a in m["Attachments"]] == ["spec.pdf"]
    hdrs = await mailpit.headers(m["ID"])
    mid_header = hdrs.get("Message-Id") or hdrs.get("Message-ID")
    assert mid_header[0] == f"<{mid}@agentbox.local>"
    assert hdrs["X-Agent-Id"] == ["a1"]
    got = (await client.get(f"/v1/messages/{mid}", headers=org.headers)).json()
    assert got["status"] == "provider_accepted" and got["provider"] == "smtp_relay" and got["sent_at"]
    assert got["attachments"][0]["sha256"] == hashlib.sha256(data).hexdigest()
    async with runtime.db.session() as s:
        [att] = (await s.scalars(select(DeliveryAttempt).where(DeliveryAttempt.message_id == mid))).all()
        assert att.status == "accepted" and att.attempt_number == 1
        assert await s.scalar(select(Event).where(Event.type == "message.provider_accepted")) is not None


async def test_temporary_failure_retries_then_fails(client, org, runtime):
    async with runtime.db.session() as s:
        s.add(ProviderAccount(id=new_id("pa"), organization_id=org.id, provider="smtp_relay", name="broken",
                              config_encrypted=encrypt_json(runtime.settings.app_secret_key,
                                                            {"host": "127.0.0.1", "port": 1, "starttls": False}),
                              status="active"))
        await s.commit()
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "t"})).json()
    r = await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                          json={"to": ["a@b.ru"], "subject": "x", "text": "x"})
    mid = r.json()["id"]
    worker = JobWorker(runtime, default_handlers(), concurrency=1, kinds=["outbound_send"])
    assert await worker.run_once() is True
    async with runtime.db.session() as s:
        job = await s.scalar(select(Job).where(Job.kind == "outbound_send"))
        assert job.status == "pending" and job.attempts == 1
        assert (await s.get(Message, mid)).status == "queued"
        [att] = (await s.scalars(select(DeliveryAttempt).where(DeliveryAttempt.message_id == mid))).all()
        assert att.status == "temporary_failure"
        job.attempts = job.max_attempts - 1
        job.run_at = job.created_at
        await s.commit()
    assert await worker.run_once() is True
    async with runtime.db.session() as s:
        m = await s.get(Message, mid)
        assert m.status == "failed" and m.error_code == "provider_temporary_failure"
        assert await s.scalar(select(Event).where(Event.type == "message.failed")) is not None
        assert (await s.scalar(select(Job).where(Job.kind == "outbound_send"))).status == "done"
