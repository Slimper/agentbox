"""Mailbox connectors through the REST API: an existing IMAP/SMTP mailbox becomes an inbox (fake IMAP, real Mailpit)."""

import json
from email.message import EmailMessage

from sqlalchemy import select

from agentbox.connectors import service as connectors
from agentbox.connectors.service import FetchedMail
from agentbox.db.models import MailboxConnection, Message
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker
from agentbox.security.crypto import decrypt_json, encrypt_json


class FakeImap:
    mails: list[FetchedMail] = []
    opened: list[dict] = []

    def __init__(self, cfg):
        FakeImap.opened.append(cfg)
        assert cfg["imap_host"] == "imap.yandex.ru" and cfg["password"] == "app-pass" and cfg["username"] == "robot@corp.ru"

    def fetch_new(self, since_uid, limit=50):
        return [m for m in FakeImap.mails if m.uid > since_uid][:limit]

    def close(self):
        pass


async def test_connect_sync_reply_disconnect(client, org, runtime, mailpit):
    r = await client.get("/v1/connections/presets", headers=org.headers)
    assert r.status_code == 200 and {p["key"] for p in r.json()["data"]} >= {"gmail", "yandex360", "m365", "imap"}

    r = await client.post("/v1/connections", headers=org.headers, json={"provider": "yandex360", "address": "Robot@Corp.ru",
                                                                        "password": "app-pass", "display_name": "Robot"})
    assert r.status_code == 201, r.text
    conn = r.json()
    inbox_id = conn["inbox"]["id"]
    assert conn["address"] == "robot@corp.ru" and conn["inbox"]["provider_mode"] == "connected"
    assert conn["imap_host"] == "imap.yandex.ru" and "app-pass" not in json.dumps(conn)
    assert (await client.get("/v1/connections", headers=org.headers)).json()["data"][0]["id"] == conn["id"]
    # validation
    assert (await client.post("/v1/connections", headers=org.headers,
                              json={"provider": "nope", "address": "a@b.c", "password": "x"})).status_code == 422
    assert (await client.post("/v1/connections", headers=org.headers,
                              json={"provider": "imap", "address": "a@b.c", "password": "x"})).status_code == 422

    m1 = EmailMessage()
    m1["From"], m1["To"], m1["Subject"], m1["Message-ID"] = "Client <client@example.com>", "robot@corp.ru", "Order #1", "<o1@example.com>"
    m1.set_content("Please confirm the order.")
    FakeImap.mails = [FetchedMail(uid=7, raw=m1.as_bytes())]
    connectors.set_client_factory(FakeImap)
    try:
        await JobWorker(runtime, default_handlers(), concurrency=1, kinds=["connector_sync", "inbound_process"]).drain()
        msgs = (await client.get(f"/v1/inboxes/{inbox_id}/messages", headers=org.headers, params={"direction": "inbound"})).json()
        assert len(msgs["data"]) == 1 and msgs["data"][0]["subject"] == "Order #1"
        inbound_id = msgs["data"][0]["id"]
        # the UID cursor advanced; a manual sync finds nothing new
        assert (await client.post(f"/v1/connections/{conn['id']}/sync", headers=org.headers)).status_code == 202
        await JobWorker(runtime, default_handlers(), concurrency=1, kinds=["connector_sync", "inbound_process"]).drain()
        assert (await client.get(f"/v1/connections/{conn['id']}", headers=org.headers)).json()["last_uid"] == 7
        assert len((await client.get(f"/v1/inboxes/{inbox_id}/messages", headers=org.headers)).json()["data"]) == 1

        # reply goes out through the mailbox's own SMTP (pointed at Mailpit here) from the mailbox address
        async with runtime.db.session() as s:
            row = await s.scalar(select(MailboxConnection).where(MailboxConnection.inbox_id == inbox_id))
            cfg = decrypt_json(runtime.settings.app_secret_key, row.config_encrypted)
            cfg.update({"smtp_host": runtime.settings.outbound_smtp_host, "smtp_port": runtime.settings.outbound_smtp_port,
                        "smtp_ssl": False, "smtp_starttls": False, "username": None, "password": None})
            row.config_encrypted = encrypt_json(runtime.settings.app_secret_key, cfg)
            await s.commit()
        r = await client.post(f"/v1/messages/{inbound_id}/reply", headers=org.headers, json={"text": "Confirmed."})
        assert r.status_code in (200, 201, 202), r.text
        await JobWorker(runtime, default_handlers(), concurrency=1, kinds=["outbound_send"]).drain()
        sent = await mailpit.find("Re: Order #1")
        assert sent["From"]["Address"] == "robot@corp.ru" and sent["ReturnPath"] == "robot@corp.ru"

        # pause / resume / disconnect
        assert (await client.post(f"/v1/connections/{conn['id']}/pause", headers=org.headers)).json()["status"] == "paused"
        assert (await client.post(f"/v1/connections/{conn['id']}/resume", headers=org.headers)).json()["status"] == "active"
        assert (await client.delete(f"/v1/connections/{conn['id']}", headers=org.headers)).status_code == 204
        assert (await client.get(f"/v1/connections/{conn['id']}", headers=org.headers)).status_code == 404
        assert (await client.get(f"/v1/inboxes/{inbox_id}", headers=org.headers)).status_code == 404
        async with runtime.db.session() as s:
            assert await s.scalar(select(Message).where(Message.inbox_id == inbox_id)) is not None  # history kept
    finally:
        connectors.set_client_factory(connectors._open_client)
