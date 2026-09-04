import json

import httpx
from sqlalchemy import select

from agentbox.db.models import Event, Job, Message, Suppression
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker


async def _inbox(client, org, username="gov"):
    return (await client.post("/v1/inboxes", headers=org.headers, json={"username": username})).json()


async def _send(client, org, inbox_id, to="sales@supplier.ru", **extra):
    return await client.post(f"/v1/inboxes/{inbox_id}/messages", headers=org.headers,
                             json={"to": [to], "subject": "s", "text": "x", **extra})


async def test_policy_crud_and_recipient_rules(client, org, runtime):
    inbox = await _inbox(client, org)
    r = await client.get("/v1/policy", headers=org.headers)
    assert r.json()["config"] == {} and r.json()["effective"]["limits"]["emails_per_day"] == 2000
    r = await client.put("/v1/policy", headers=org.headers,
                         json={"recipient_policy": {"blocked_domains": ["spam.com"]}})
    assert r.status_code == 200 and r.json()["effective"]["recipient_policy"]["blocked_domains"] == ["spam.com"]
    assert (await client.put("/v1/policy", headers=org.headers, json={"bogus": 1})).status_code == 422
    r = await client.put(f"/v1/inboxes/{inbox['id']}/policy", headers=org.headers,
                         json={"recipient_policy": {"allowed_domains": ["supplier.ru"]}})
    assert r.json()["effective"]["recipient_policy"] == {"allowed_domains": ["supplier.ru"],
                                                         "blocked_domains": ["spam.com"]}
    r = await _send(client, org, inbox["id"], to="x@spam.com")
    assert r.status_code == 422 and r.json()["error"]["code"] == "recipient_blocked"
    r = await _send(client, org, inbox["id"], to="x@other.ru")
    assert r.status_code == 422 and r.json()["error"]["details"]["reason"] == "domain_not_allowed"
    assert (await _send(client, org, inbox["id"], to="ok@mail.supplier.ru")).status_code == 202
    async with runtime.db.session() as s:
        blocked = (await s.scalars(select(Event).where(Event.type == "policy.blocked"))).all()
        assert len(blocked) == 2 and blocked[0].payload["reason"] == "blocked_domain"
    assert (await client.delete(f"/v1/inboxes/{inbox['id']}/policy", headers=org.headers)).status_code == 204
    assert (await _send(client, org, inbox["id"], to="x@other.ru")).status_code == 202


async def test_limits_executables_and_send_disabled(client, org, runtime):
    inbox = await _inbox(client, org)
    await client.put(f"/v1/inboxes/{inbox['id']}/policy", headers=org.headers,
                     json={"limits": {"emails_per_minute": 2, "per_thread_per_hour": 1}})
    first = await _send(client, org, inbox["id"])
    assert first.status_code == 202
    r = await client.post(f"/v1/messages/{first.json()['id']}/reply", headers=org.headers, json={"text": "again"})
    assert r.status_code == 429 and r.json()["error"]["details"]["reason"] == "possible_automation_loop"
    assert (await _send(client, org, inbox["id"], to="two@supplier.ru")).status_code == 202
    r = await _send(client, org, inbox["id"], to="three@supplier.ru")
    assert r.status_code == 429 and r.json()["error"]["details"]["limit"] == "emails_per_minute"
    up = (await client.post("/v1/attachments/uploads", headers=org.headers,
                            json={"filename": "virus.exe", "size_bytes": 3})).json()
    other = await _inbox(client, org, "gov2")
    r = await _send(client, org, other["id"], attachment_ids=[up["attachment_id"]])
    assert r.status_code == 422 and r.json()["error"]["details"]["reason"] == "executable_attachment"
    await client.put("/v1/policy", headers=org.headers, json={"send_enabled": False})
    r = await _send(client, org, other["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "inbox_disabled"


async def test_receive_disabled_rejects_at_smtp(client, org, smtp_edge):
    from email.message import EmailMessage

    import aiosmtplib
    import pytest

    inbox = await _inbox(client, org, "norecv")
    await client.put(f"/v1/inboxes/{inbox['id']}/policy", headers=org.headers, json={"receive_enabled": False})
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@b.ru", inbox["email"], "x"
    m.set_content("hi")
    host, port = smtp_edge
    with pytest.raises(aiosmtplib.SMTPRecipientsRefused):
        await aiosmtplib.send(m, sender="a@b.ru", recipients=[inbox["email"]], hostname=host, port=port,
                              start_tls=False)


async def test_suppressions_manual_and_auto(client, org, runtime, smtp_edge):
    import aiosmtplib

    inbox = await _inbox(client, org, "sup")
    r = await client.post("/v1/suppressions", headers=org.headers, json={"email": "Bad@Supplier.ru", "note": "nope"})
    assert r.status_code == 201 and r.json()["email"] == "bad@supplier.ru" and r.json()["reason"] == "manual"
    r = await _send(client, org, inbox["id"], to="bad@supplier.ru")
    assert r.status_code == 422 and r.json()["error"]["code"] == "suppressed_recipient"
    sup_id = (await client.get("/v1/suppressions", headers=org.headers)).json()["data"][0]["id"]
    assert (await client.delete(f"/v1/suppressions/{sup_id}", headers=org.headers)).status_code == 204
    assert (await _send(client, org, inbox["id"], to="bad@supplier.ru")).status_code == 202
    # hard bounce via DSN adds a suppression automatically
    sent = (await _send(client, org, inbox["id"], to="gone@supplier.ru")).json()
    async with runtime.db.session() as s:
        (await s.get(Message, sent["id"])).status = "provider_accepted"
        await s.commit()
    rcpt = f"bounce+{sent['id'].split('_', 1)[1].lower()}@agentbox.local"
    dsn = (b"From: MAILER-DAEMON@relay\r\nTo: x\r\nSubject: Undelivered\r\nMIME-Version: 1.0\r\n"
           b'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n\r\n'
           b"--B\r\nContent-Type: text/plain\r\n\r\nfailed\r\n--B\r\nContent-Type: message/delivery-status\r\n\r\n"
           b"Reporting-MTA: dns; relay\r\n\r\nFinal-Recipient: rfc822; gone@supplier.ru\r\nAction: failed\r\n"
           b"Status: 5.1.1\r\n\r\n--B--\r\n")
    host, port = smtp_edge
    await aiosmtplib.send(dsn, sender="mailer-daemon@relay", recipients=[rcpt], hostname=host, port=port,
                          start_tls=False)
    await JobWorker(runtime, default_handlers(), concurrency=1, kinds=["inbound_process"]).drain()
    r = await client.get("/v1/suppressions", headers=org.headers, params={"email": "gone@supplier.ru"})
    [sup] = r.json()["data"]
    assert sup["reason"] == "hard_bounce" and sup["source"] == "dsn"
    r = await _send(client, org, inbox["id"], to="gone@supplier.ru")
    assert r.status_code == 422 and r.json()["error"]["code"] == "suppressed_recipient"


async def test_approval_gate_flow(client, org, runtime):
    inbox = await _inbox(client, org, "appr")
    await client.put("/v1/policy", headers=org.headers, json={"approval": {"external_domain": True}})
    r = await _send(client, org, inbox["id"], to="vendor@external.com")
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    mid = r.json()["id"]
    async with runtime.db.session() as s:
        assert await s.scalar(select(Job).where(Job.kind == "outbound_send")) is None
        assert await s.scalar(select(Event).where(Event.type == "approval.required")) is not None
    pending = (await client.get("/v1/approvals", headers=org.headers)).json()["data"]
    assert [m["id"] for m in pending] == [mid]
    # internal recipient needs no approval
    r = await _send(client, org, inbox["id"], to="other@agentbox.local")
    assert r.json()["status"] == "queued"
    r = await client.post(f"/v1/messages/{mid}/approve", headers=org.headers)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert (await client.post(f"/v1/messages/{mid}/approve", headers=org.headers)).status_code == 409
    async with runtime.db.session() as s:
        jobs = (await s.scalars(select(Job).where(Job.kind == "outbound_send"))).all()
        assert len(jobs) == 2
    r = await _send(client, org, inbox["id"], to="second@external.com")
    r2 = await client.post(f"/v1/messages/{r.json()['id']}/reject", headers=org.headers, json={"reason": "no"})
    assert r2.json()["status"] == "rejected"
    m = (await client.get(f"/v1/messages/{r.json()['id']}", headers=org.headers)).json()
    assert m["error_code"] == "rejected_by_approver" and m["error_message"] == "no"
    # new_recipient gate: known recipient passes, unknown needs approval
    await client.put("/v1/policy", headers=org.headers, json={"approval": {"new_recipient": True}})
    assert (await _send(client, org, inbox["id"], to="vendor@external.com")).json()["status"] == "queued"
    assert (await _send(client, org, inbox["id"], to="fresh@external.com")).json()["status"] == "pending_approval"


async def test_provider_accounts_routing_and_sendgrid_events(client, org, runtime):
    inbox = await _inbox(client, org, "route")
    smtp_cfg = {"host": runtime.settings.outbound_smtp_host, "port": runtime.settings.outbound_smtp_port}
    r = await client.post("/v1/provider-accounts", headers=org.headers,
                          json={"provider": "smtp_relay", "name": "own relay", "config": smtp_cfg})
    assert r.status_code == 201 and r.json()["config"] == smtp_cfg
    r = await client.post("/v1/provider-accounts", headers=org.headers,
                          json={"provider": "sendgrid", "name": "sg", "config": {"api_key": "SG.secret",
                                                                                   "base_url": "https://sg.test"}})
    assert r.status_code == 201, r.text
    acc = r.json()
    assert "api_key" not in json.dumps(acc["config"]) and acc["config_keys"] == ["api_key", "base_url"]
    assert acc["events_url"].endswith(f"/v1/providers/sendgrid/events/{acc['events_url'].rsplit('/', 1)[1]}")
    assert (await client.post("/v1/provider-accounts", headers=org.headers,
                              json={"provider": "sendgrid", "name": "bad", "config": {}})).status_code == 422
    r = await client.post("/v1/routing-rules", headers=org.headers,
                          json={"priority": 10, "match": {"recipient_domain_suffix": "com"},
                                "provider_account_id": acc["id"]})
    assert r.status_code == 201
    assert (await client.post("/v1/routing-rules", headers=org.headers,
                              json={"match": {"nope": 1}, "provider_account_id": acc["id"]})).status_code == 422
    # .com goes to sendgrid (mocked), .ru goes to the default smtp relay (mailpit)
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, headers={"X-Message-Id": "sg-42"})

    real_http = runtime.http
    runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        com = (await _send(client, org, inbox["id"], to="v@example.com")).json()
        ru = (await _send(client, org, inbox["id"], to="v@example.ru")).json()
        await JobWorker(runtime, default_handlers(), concurrency=1, kinds=["outbound_send"]).drain()
    finally:
        runtime.http = real_http
    assert len(captured) == 1 and captured[0]["custom_args"]["agentbox_message_id"] == com["id"]
    m_com = (await client.get(f"/v1/messages/{com['id']}", headers=org.headers)).json()
    m_ru = (await client.get(f"/v1/messages/{ru['id']}", headers=org.headers)).json()
    assert m_com["provider"] == "sendgrid" and m_com["provider_message_id"] == "sg-42"
    assert m_ru["provider"] == "smtp_relay"
    # provider events normalize into delivered / bounced (+ suppression) via the token URL
    events_url = acc["events_url"].replace("http://localhost:8000", "")
    payload = [{"event": "delivered", "email": "v@example.com", "agentbox_message_id": com["id"], "sg_event_id": "e1"},
               {"event": "bounce", "email": "v@example.com", "agentbox_message_id": com["id"], "status": "5.0.0",
                "reason": "mailbox full"},
               {"event": "delivered", "email": "x@y", "agentbox_message_id": "msg_UNKNOWN"}]
    r = await client.post(events_url, json=payload)
    assert r.status_code == 200 and r.json() == {"received": 3, "applied": 2}
    m_com = (await client.get(f"/v1/messages/{com['id']}", headers=org.headers)).json()
    assert m_com["status"] == "bounced" and m_com["error_code"] == "5.0.0"
    async with runtime.db.session() as s:
        assert await s.scalar(select(Suppression).where(Suppression.email == "v@example.com")) is not None
        types = sorted((await s.scalars(select(Event.type).where(Event.resource_id == com["id"]))).all())
        assert "message.delivered" in types and "message.bounced" in types
    assert (await client.post("/v1/providers/sendgrid/events/wrong-token", json=[])).status_code == 404
    # analytics
    r = await client.get("/v1/analytics/delivery", headers=org.headers, params={"group_by": "provider"})
    by = {row["key"]: row for row in r.json()["data"]}
    assert by["sendgrid"]["sent"] == 1 and by["sendgrid"]["bounced"] == 1 and by["smtp_relay"]["provider_accepted"] == 1
    r = await client.get("/v1/analytics/delivery", headers=org.headers, params={"group_by": "recipient_domain"})
    assert {row["key"] for row in r.json()["data"]} == {"example.com", "example.ru"}
    # deleting the account removes its rules
    assert (await client.delete(f"/v1/provider-accounts/{acc['id']}", headers=org.headers)).status_code == 204
    assert (await client.get("/v1/routing-rules", headers=org.headers)).json()["data"] == []
