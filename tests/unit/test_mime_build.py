from datetime import UTC, datetime

from agentbox.mime.build import OutboundAttachment, OutboundMessage, build_mime, format_address
from agentbox.mime.parse import Address, parse_mime


def test_build_roundtrip():
    m = OutboundMessage(
        message_id="<msg_1@agentbox.local>", from_=Address("agent@agentbox.local", "Агент"),
        to=[Address("sales@supplier.ru", "Sales")], cc=[Address("cc@supplier.ru")], bcc=[Address("hidden@x.ru")],
        reply_to=[], subject="Запрос КП\r\nX-Injected: 1", text="Привет", html="<p>Привет <img src='cid:logo'></p>",
        in_reply_to="<prev@x>", references=["<root@x>", "<prev@x>"], headers=[["X-Agent-Id", "a1"]],
        attachments=[OutboundAttachment("offer.pdf", "application/pdf", b"%PDF"),
                     OutboundAttachment("logo.png", "image/png", b"\x89PNG", disposition="inline", content_id="logo")],
        date=datetime(2026, 9, 1, tzinfo=UTC),
    )
    raw = build_mime(m)
    assert b"hidden@x.ru" not in raw
    p = parse_mime(raw)
    assert p.message_id == "<msg_1@agentbox.local>" and p.in_reply_to == "<prev@x>"
    assert p.references == ["<root@x>", "<prev@x>"]
    assert p.subject == "Запрос КП X-Injected: 1"
    assert p.from_[0].name == "Агент" and p.to[0].email == "sales@supplier.ru" and p.cc[0].email == "cc@supplier.ru"
    assert p.text.strip() == "Привет" and "<p>" in p.html
    assert ("X-Agent-Id", "a1") in p.headers
    names = sorted(a.filename for a in p.attachments)
    assert names == ["logo.png", "offer.pdf"]
    inline = [a for a in p.attachments if a.disposition == "inline"][0]
    assert inline.content_id == "logo"


def test_text_only_and_format_address():
    m = OutboundMessage("<a@b>", Address("a@b.ru"), [Address("c@d.ru")], [], [], [], "s", "body", None, None, [],
                        [], [])
    p = parse_mime(build_mime(m))
    assert p.text.strip() == "body" and p.html is None and p.attachments == []
    assert format_address(Address("a@b.ru", 'Q "x"')) == '"Q \\"x\\"" <a@b.ru>'
