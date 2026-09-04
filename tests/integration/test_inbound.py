from email.message import EmailMessage

import aiosmtplib
import pytest
from sqlalchemy import select

from agentbox.db.models import Event, InboundIngest, Message
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker

DSN_TEMPLATE = """From: MAILER-DAEMON@relay.example
To: {rcpt}
Subject: Undelivered Mail Returned to Sender
Content-Type: multipart/report; report-type=delivery-status; boundary="B"
MIME-Version: 1.0

--B
Content-Type: text/plain

failed
--B
Content-Type: message/delivery-status

Reporting-MTA: dns; relay.example

Final-Recipient: rfc822; nobody@supplier.ru
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 User unknown

--B--
"""


async def _deliver(edge, sender, rcpt, msg) -> tuple:
    host, port = edge
    return await aiosmtplib.send(msg, sender=sender, recipients=[rcpt], hostname=host, port=port, start_tls=False)


def _vendor_reply(to: str, in_reply_to: str | None = None, subject="Re: Quote", with_pdf=True) -> EmailMessage:
    m = EmailMessage()
    m["From"] = "Sales <sales@supplier.ru>"
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = f"<{subject.replace(' ', '')}-{to}@supplier.ru>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content("Добрый день, наше предложение во вложении.")
    if with_pdf:
        m.add_attachment(b"%PDF-1.4 offer", maintype="application", subtype="pdf", filename="offer.pdf")
    return m


async def test_rcpt_policy(client, org, smtp_edge):
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "rcpt"})).json()
    with pytest.raises(aiosmtplib.SMTPRecipientsRefused):
        await _deliver(smtp_edge, "a@b.ru", "nobody@agentbox.local", _vendor_reply("nobody@agentbox.local"))
    await client.post(f"/v1/inboxes/{inbox['id']}/disable", headers=org.headers)
    with pytest.raises(aiosmtplib.SMTPRecipientsRefused):
        await _deliver(smtp_edge, "a@b.ru", inbox["email"], _vendor_reply(inbox["email"]))


async def test_inbound_message_stored_threaded_with_attachment(client, org, runtime, smtp_edge):
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "proc"})).json()
    sent = (await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                              json={"to": ["sales@supplier.ru"], "subject": "Quote", "text": "hi"})).json()
    original = (await client.get(f"/v1/messages/{sent['id']}", headers=org.headers)).json()
    errors, response = await _deliver(smtp_edge, "sales@supplier.ru", inbox["email"],
                                      _vendor_reply(inbox["email"], in_reply_to=original["internet_message_id"]))
    assert "Queued as ing_" in response
    async with runtime.db.session() as s:
        ingest = await s.scalar(select(InboundIngest))
        assert ingest.status == "received" and ingest.kind == "message"
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    msgs = (await client.get(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                             params={"direction": "inbound"})).json()["data"]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["thread_id"] == sent["thread_id"] and m["status"] == "stored"
    assert m["from"] == {"email": "sales@supplier.ru", "name": "Sales"}
    assert "во вложении" in m["text"] and m["attachments"][0]["filename"] == "offer.pdf"
    thread = (await client.get(f"/v1/threads/{sent['thread_id']}", headers=org.headers)).json()
    assert [x["direction"] for x in thread["messages"]] == ["outbound", "inbound"] and thread["message_count"] == 2
    async with runtime.db.session() as s:
        ev = await s.scalar(select(Event).where(Event.type == "message.received"))
        assert ev.payload["inbox_id"] == inbox["id"] and ev.payload["message"]["truncated"] is False
        assert ev.payload["message"]["attachments"][0]["filename"] == "offer.pdf"
        assert (await s.scalar(select(InboundIngest))).status == "stored"
    dl = (await client.get(f"/v1/attachments/{m['attachments'][0]['id']}/download", headers=org.headers)).json()
    assert dl["url"].startswith("http")


async def test_duplicate_message_id_and_subject_fallback_threading(client, org, runtime, smtp_edge):
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "dupe"})).json()
    msg = _vendor_reply(inbox["email"], subject="Fresh topic", with_pdf=False)
    await _deliver(smtp_edge, "sales@supplier.ru", inbox["email"], msg)
    await _deliver(smtp_edge, "sales@supplier.ru", inbox["email"], msg)
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    async with runtime.db.session() as s:
        statuses = sorted((await s.scalars(select(InboundIngest.status))).all())
        assert statuses == ["duplicate", "stored"]
    follow = _vendor_reply(inbox["email"], subject="Re: Fresh topic", with_pdf=False)
    del follow["Message-ID"]
    follow["Message-ID"] = "<second@supplier.ru>"
    await _deliver(smtp_edge, "sales@supplier.ru", inbox["email"], follow)
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    threads = (await client.get(f"/v1/inboxes/{inbox['id']}/threads", headers=org.headers)).json()["data"]
    assert len(threads) == 1 and threads[0]["message_count"] == 2


async def test_bounce_dsn_marks_message_bounced(client, org, runtime, smtp_edge):
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "b"})).json()
    sent = (await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                              json={"to": ["nobody@supplier.ru"], "subject": "x", "text": "x"})).json()
    async with runtime.db.session() as s:
        m = await s.get(Message, sent["id"])
        m.status = "provider_accepted"
        await s.commit()
    rcpt = f"bounce+{sent['id'].split('_', 1)[1].lower()}@agentbox.local"
    raw = DSN_TEMPLATE.format(rcpt=rcpt).replace("\n", "\r\n").encode()
    host, port = smtp_edge
    await aiosmtplib.send(raw, sender="mailer-daemon@relay.example", recipients=[rcpt], hostname=host, port=port,
                          start_tls=False)
    with pytest.raises(aiosmtplib.SMTPRecipientsRefused):
        await aiosmtplib.send(raw, sender="mailer-daemon@relay.example",
                              recipients=["bounce+00000000000000000000000000@agentbox.local"],
                              hostname=host, port=port, start_tls=False)
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    m = (await client.get(f"/v1/messages/{sent['id']}", headers=org.headers)).json()
    assert m["status"] == "bounced" and m["error_code"] == "5.1.1"
    inbound = (await client.get(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                                params={"direction": "inbound"})).json()["data"]
    assert inbound == []
    async with runtime.db.session() as s:
        ev = await s.scalar(select(Event).where(Event.type == "message.bounced"))
        assert ev.payload["bounce"]["recipients"][0]["recipient"] == "nobody@supplier.ru"
