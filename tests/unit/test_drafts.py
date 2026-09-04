from datetime import UTC, datetime

from agentbox.db.models import Inbox, Message
from agentbox.services.messages import build_forward_draft, build_reply_draft, truncate_body


def _msg(**kw):
    base = dict(id="msg_1", organization_id="org", inbox_id="ibx", thread_id="thr", direction="inbound",
                status="stored", from_address={"email": "sales@s.ru", "name": "Sales"},
                to_addresses=[{"email": "agent@agentbox.local"}, {"email": "boss@c.ru"}],
                cc_addresses=[{"email": "cc@s.ru"}], bcc_addresses=[], reply_to_addresses=[],
                subject="Re: Re: Quote", text_body="hello", html_body=None,
                internet_message_id="<abc@s.ru>", in_reply_to=None, references=["<root@x>"], headers=[],
                created_at=datetime(2026, 9, 1, tzinfo=UTC))
    base.update(kw)
    return Message(**base)


INBOX = Inbox(id="ibx", organization_id="org", address="agent@agentbox.local", username="agent", domain_id="d")


def test_reply_defaults_to_from_and_reply_all_adds_others():
    d = build_reply_draft(_msg(), INBOX, text="ok", html=None, reply_all=False, to=[], cc=[], bcc=[], attachment_ids=[])
    assert [a["email"] for a in d.to] == ["sales@s.ru"] and d.cc == []
    assert d.subject == "Re: Quote" and d.in_reply_to == "<abc@s.ru>" and d.references == ["<root@x>", "<abc@s.ru>"]
    assert d.thread_id == "thr"
    d = build_reply_draft(_msg(), INBOX, text="ok", html=None, reply_all=True, to=[], cc=[], bcc=[], attachment_ids=[])
    assert sorted(a["email"] for a in d.cc) == ["boss@c.ru", "cc@s.ru"]


def test_reply_prefers_reply_to_and_outbound_replies_to_recipients():
    m = _msg(reply_to_addresses=[{"email": "rt@s.ru"}])
    d = build_reply_draft(m, INBOX, text="x", html=None, reply_all=False, to=[], cc=[], bcc=[], attachment_ids=[])
    assert [a["email"] for a in d.to] == ["rt@s.ru"]
    m = _msg(direction="outbound", from_address={"email": "agent@agentbox.local"},
             to_addresses=[{"email": "sales@s.ru"}])
    d = build_reply_draft(m, INBOX, text="x", html=None, reply_all=False, to=[], cc=[], bcc=[], attachment_ids=[])
    assert [a["email"] for a in d.to] == ["sales@s.ru"]


def test_forward_quotes_original():
    d = build_forward_draft(_msg(), to=[{"email": "acc@c.ru"}], cc=[], bcc=[], text="FYI", html=None,
                            include_attachments=True)
    assert d.subject == "Fwd: Quote" and d.thread_id is None
    assert d.text.startswith("FYI\n\n---------- Forwarded message ----------\nFrom: Sales <sales@s.ru>")
    assert d.text.endswith("hello") and d.html is None and d.forward_attachments_from == "msg_1"


def test_truncate_body():
    assert truncate_body(None) == (None, False)
    assert truncate_body("x" * 10, limit=4) == ("xxxx", True)
