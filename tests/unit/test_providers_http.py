import json

import httpx
import pytest

from agentbox.db.models import RoutingRule
from agentbox.mime.build import OutboundAttachment, OutboundMessage
from agentbox.mime.parse import Address
from agentbox.providers.base import Envelope, PermanentError, TemporaryError
from agentbox.providers.router import rule_matches
from agentbox.providers.sendgrid import SendGridProvider
from agentbox.providers.unisender import UnisenderGoProvider

MSG = OutboundMessage(
    message_id="<msg_01X@agentbox.local>", from_=Address("agent@agentbox.local", "Agent"),
    to=[Address("a@b.ru", "A")], cc=[Address("c@b.ru")], bcc=[], reply_to=[Address("r@b.ru")], subject="Hi",
    text="t", html="<p>t</p>", in_reply_to="<x@y>", references=["<x@y>"], headers=[["X-Agent", "1"]],
    attachments=[OutboundAttachment("a.pdf", "application/pdf", b"%PDF"),
                 OutboundAttachment("l.png", "image/png", b"\x89", disposition="inline", content_id="logo")],
)
ENV = Envelope(mail_from="bounce+01x@agentbox.local", rcpt_to=["a@b.ru", "c@b.ru"], message_id=MSG.message_id)


def test_sendgrid_payload():
    p = SendGridProvider("k", httpx.AsyncClient()).build_payload(MSG, "msg_01X")
    assert p["personalizations"][0]["to"] == [{"email": "a@b.ru", "name": "A"}]
    assert p["personalizations"][0]["cc"] == [{"email": "c@b.ru"}]
    assert p["headers"]["Message-ID"] == "<msg_01X@agentbox.local>" and p["headers"]["In-Reply-To"] == "<x@y>"
    assert p["custom_args"] == {"agentbox_message_id": "msg_01X"} and p["reply_to"] == {"email": "r@b.ru"}
    assert [a["filename"] for a in p["attachments"]] == ["a.pdf", "l.png"]
    assert p["attachments"][1]["content_id"] == "logo"


async def test_sendgrid_send_outcomes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        code = {"Hi": 202, "tmp": 503, "bad": 400}[body["subject"]]
        return httpx.Response(code, headers={"X-Message-Id": "sg-1"}, text="{}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = SendGridProvider("key", http, base_url="https://sg.test")
    ok = await p.send(ENV, MSG, b"")
    assert ok.accepted and ok.provider_message_id == "sg-1"
    assert calls[0].headers["Authorization"] == "Bearer key" and calls[0].url.path == "/v3/mail/send"
    assert json.loads(calls[0].content)["custom_args"]["agentbox_message_id"] == "msg_01X"
    with pytest.raises(TemporaryError):
        await p.send(ENV, OutboundMessage(**{**MSG.__dict__, "subject": "tmp"}), b"")
    with pytest.raises(PermanentError):
        await p.send(ENV, OutboundMessage(**{**MSG.__dict__, "subject": "bad"}), b"")


def test_sendgrid_events():
    evs = SendGridProvider.parse_events([
        {"event": "delivered", "email": "A@b.ru", "agentbox_message_id": "msg_1", "sg_event_id": "e1", "timestamp": 1},
        {"event": "bounce", "email": "x@b.ru", "agentbox_message_id": "msg_2", "status": "5.1.1", "reason": "no user"},
        {"event": "open", "agentbox_message_id": "msg_3"},
    ])
    assert [(e.status, e.recipient, e.agentbox_message_id) for e in evs] == [
        ("delivered", "a@b.ru", "msg_1"), ("bounced", "x@b.ru", "msg_2")]
    assert evs[1].reason_code == "5.1.1"


def test_unisender_payload_and_events():
    p = UnisenderGoProvider("k", httpx.AsyncClient()).build_payload(MSG, "msg_01X")["message"]
    assert [r["email"] for r in p["recipients"]] == ["a@b.ru", "c@b.ru"]
    assert p["headers"]["CC"] == "c@b.ru" and p["global_metadata"] == {"agentbox_message_id": "msg_01X"}
    assert p["attachments"][0]["name"] == "a.pdf" and p["inline_attachments"][0]["name"] == "logo"
    evs = UnisenderGoProvider.parse_events({"events_by_user": [{"events": [
        {"event_name": "transactional_email_status", "event_data": {
            "email": "A@b.ru", "status": "hard_bounced", "job_id": "j1", "metadata": {"agentbox_message_id": "msg_9"},
            "delivery_info": {"destination_response_code": "550", "destination_response": "user unknown"}}},
        {"event_name": "transactional_spam_block", "event_data": {}},
    ]}]})
    assert len(evs) == 1 and evs[0].status == "bounced" and evs[0].reason_code == "550" and evs[0].recipient == "a@b.ru"


async def test_unisender_send():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == "key"
        return httpx.Response(200, json={"status": "success", "job_id": "job-1",
                                         "failed_emails": {"c@b.ru": "invalid"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = UnisenderGoProvider("key", http, base_url="https://u.test")
    r = await p.send(ENV, MSG, b"")
    assert r.accepted and r.provider_message_id == "job-1" and r.refused == {"c@b.ru": "invalid"}


def test_rule_matches():
    r = RoutingRule(match={"recipient_domain_suffix": "ru"})
    assert rule_matches(r, inbox_id="i", recipient_domain="mail.ru") and not rule_matches(r, inbox_id="i",
                                                                                          recipient_domain="x.com")
    r = RoutingRule(match={"inbox_id": "ibx_1", "recipient_domain_suffix": ".supplier.ru"})
    assert rule_matches(r, inbox_id="ibx_1", recipient_domain="supplier.ru")
    assert not rule_matches(r, inbox_id="ibx_2", recipient_domain="supplier.ru")
    assert rule_matches(RoutingRule(match={}), inbox_id=None, recipient_domain=None)
