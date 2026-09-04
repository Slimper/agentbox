"""Acceptance walk-through (design spec §12): create inbox → send → external reply → webhook → thread →
attachment → reply → long-poll → bounce."""

import asyncio
import hashlib
from email.message import EmailMessage

import aiosmtplib
import httpx

from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker
from agentbox.webhooks.signing import verify_signature

PDF = b"%PDF-1.4 commercial offer " * 50


async def _wait_for(pred, timeout=15.0, interval=0.2):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = pred()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


def _received(listener, type_):
    return [r for r in listener.received if r["json"]["type"] == type_]


async def test_full_acceptance(client, org, runtime, mailpit, webhook_listener, smtp_edge):
    stop = asyncio.Event()
    worker = JobWorker(runtime, default_handlers(), concurrency=2, poll_interval=0.1)
    worker_task = asyncio.create_task(worker.run(stop))
    try:
        # 12. register a webhook
        hook = (await client.post("/v1/webhooks", headers=org.headers,
                                  json={"url": webhook_listener.url,
                                        "event_types": ["message.received", "message.provider_accepted",
                                                        "message.bounced"]})).json()
        # 3. create inbox
        inbox = (await client.post("/v1/inboxes", headers=org.headers,
                                   json={"username": "agent", "display_name": "Procurement Agent"})).json()
        assert inbox["email"] == "agent@agentbox.local"

        # 4. send to the outside world (Mailpit)
        send_body = {"to": [{"email": "vendor@example.com", "name": "Vendor"}],
                     "subject": "Запрос коммерческого предложения", "text": "Пришлите КП.",
                     "html": "<p>Пришлите КП.</p>"}
        send_headers = {**org.headers, "Idempotency-Key": "send-1"}
        sent = (await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=send_headers, json=send_body)).json()
        # 17. duplicate send is idempotent
        dup = await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=send_headers, json=send_body)
        assert dup.json()["id"] == sent["id"] and dup.headers["Idempotent-Replayed"] == "true"

        # 5. provider accepted (webhook) and mail visible in Mailpit
        outside = await mailpit.find("Запрос коммерческого предложения")
        assert outside["From"]["Address"] == "agent@agentbox.local"
        await _wait_for(lambda: _received(webhook_listener, "message.provider_accepted"))
        m = (await client.get(f"/v1/messages/{sent['id']}", headers=org.headers)).json()
        assert m["status"] == "provider_accepted"

        # 6. vendor replies from the external mailbox (SMTP into our edge) with a PDF
        reply = EmailMessage()
        reply["From"] = "Vendor <vendor@example.com>"
        reply["To"] = inbox["email"]
        reply["Subject"] = "Re: Запрос коммерческого предложения"
        reply["Message-ID"] = "<offer-1@example.com>"
        reply["In-Reply-To"] = m["internet_message_id"]
        reply["References"] = m["internet_message_id"]
        reply.set_content("Добрый день! КП во вложении.")
        reply.add_attachment(PDF, maintype="application", subtype="pdf", filename="offer.pdf")
        host, port = smtp_edge
        await aiosmtplib.send(reply, sender="vendor@example.com", recipients=[inbox["email"]],
                              hostname=host, port=port, start_tls=False)

        # 7. message.received webhook arrives, signed
        received = await _wait_for(lambda: _received(webhook_listener, "message.received"))
        evt = received[0]
        assert verify_signature(hook["secret"], evt["headers"]["AgentBox-Signature"], evt["body"])
        inbound_id = evt["json"]["data"]["message_id"]

        # 8 + 9. read the reply through the API; both messages in one thread
        inbound = (await client.get(f"/v1/messages/{inbound_id}", headers=org.headers)).json()
        assert inbound["thread_id"] == sent["thread_id"] and "во вложении" in inbound["text"]
        thread = (await client.get(f"/v1/threads/{sent['thread_id']}", headers=org.headers)).json()
        assert [x["direction"] for x in thread["messages"]] == ["outbound", "inbound"]

        # 10. download the attachment
        att = inbound["attachments"][0]
        dl = (await client.get(f"/v1/attachments/{att['id']}/download", headers=org.headers)).json()
        async with httpx.AsyncClient() as ext:
            blob = (await ext.get(dl["url"])).content
        assert hashlib.sha256(blob).hexdigest() == att["sha256"] == hashlib.sha256(PDF).hexdigest()

        # 11. reply through the API; lands in Mailpit with proper threading headers
        rep = (await client.post(f"/v1/messages/{inbound_id}/reply", headers=org.headers,
                                 json={"text": "Спасибо! Уточните срок поставки."})).json()
        assert rep["thread_id"] == sent["thread_id"]
        outside_reply = await mailpit.find("Re: Запрос коммерческого предложения")
        hdrs = await mailpit.headers(outside_reply["ID"])
        assert hdrs["In-Reply-To"] == ["<offer-1@example.com>"]
        assert hdrs["References"][0].split()[-1] == "<offer-1@example.com>"
        assert outside_reply["To"][0]["Address"] == "vendor@example.com"

        # long-poll returns the inbound message immediately
        lp = (await client.get(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                               params={"direction": "inbound", "wait": 5})).json()
        assert [x["id"] for x in lp["data"]] == [inbound_id]

        # 19. a DSN bounce for the first message normalizes to message.bounced
        dsn = (
            "From: MAILER-DAEMON@relay\r\nTo: {rcpt}\r\nSubject: Undelivered\r\nMIME-Version: 1.0\r\n"
            'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n\r\n'
            "--B\r\nContent-Type: text/plain\r\n\r\nfailed\r\n"
            "--B\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; relay\r\n\r\n"
            "Final-Recipient: rfc822; vendor@example.com\r\nAction: failed\r\nStatus: 5.1.1\r\n\r\n--B--\r\n"
        )
        rcpt = f"bounce+{sent['id'].split('_', 1)[1].lower()}@agentbox.local"
        await aiosmtplib.send(dsn.format(rcpt=rcpt).encode(), sender="mailer-daemon@relay", recipients=[rcpt],
                              hostname=host, port=port, start_tls=False)
        await _wait_for(lambda: _received(webhook_listener, "message.bounced"))
        assert (await client.get(f"/v1/messages/{sent['id']}", headers=org.headers)).json()["status"] == "bounced"

        # 20. lifecycle visible through the events API
        events = (await client.get("/v1/events", headers=org.headers, params={"limit": 100})).json()["data"]
        types = {e["type"] for e in events}
        assert {"inbox.created", "message.queued", "message.provider_accepted", "message.received",
                "message.bounced"} <= types
    finally:
        stop.set()
        await worker_task
