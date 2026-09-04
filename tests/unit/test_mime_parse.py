from email.message import EmailMessage

from agentbox.mime.parse import html_to_text, parse_mime


def _mixed_with_pdf() -> bytes:
    m = EmailMessage()
    m["From"] = "Иван Петров <ivan@supplier.ru>"
    m["To"] = "procurement-agent@agentbox.local, Someone <x@y.ru>"
    m["Cc"] = "cc@y.ru"
    m["Subject"] = "Re: Запрос КП"
    m["Message-ID"] = "<abc@supplier.ru>"
    m["In-Reply-To"] = "<msg_1@agentbox.local>"
    m["References"] = "<root@agentbox.local> <msg_1@agentbox.local>"
    m["Date"] = "Tue, 01 Sep 2026 10:00:00 +0300"
    m.set_content("Добрый день, во вложении.")
    m.add_alternative("<p>Добрый день, <b>во вложении</b>.</p>", subtype="html")
    m.add_attachment(b"%PDF-1.4 hello", maintype="application", subtype="pdf", filename="offer.pdf")
    return m.as_bytes()


def test_parse_mixed_with_pdf():
    p = parse_mime(_mixed_with_pdf())
    assert p.message_id == "<abc@supplier.ru>"
    assert p.in_reply_to == "<msg_1@agentbox.local>"
    assert p.references == ["<root@agentbox.local>", "<msg_1@agentbox.local>"]
    assert p.subject == "Re: Запрос КП"
    assert p.from_[0].email == "ivan@supplier.ru" and p.from_[0].name == "Иван Петров"
    assert [a.email for a in p.to] == ["procurement-agent@agentbox.local", "x@y.ru"]
    assert p.cc[0].email == "cc@y.ru"
    assert p.date is not None and p.date.hour == 10
    assert "во вложении" in p.text and "<b>" in p.html
    assert [a.filename for a in p.attachments] == ["offer.pdf"]
    assert p.attachments[0].content == b"%PDF-1.4 hello" and p.attachments[0].content_type == "application/pdf"
    assert ("Subject", "Re: Запрос КП") in p.headers


def test_parse_inline_image_and_html_only():
    m = EmailMessage()
    m["From"] = "a@b.ru"
    m["To"] = "c@d.ru"
    m["Subject"] = "pic"
    m.set_content("<html><body><p>Hi <img src='cid:img1'></p><script>x</script></body></html>", subtype="html")
    m.add_related(b"\x89PNG...", maintype="image", subtype="png", cid="<img1>", filename="a.png")
    p = parse_mime(m.as_bytes())
    assert p.html and p.text.strip() == "Hi"
    assert len(p.attachments) == 1
    assert p.attachments[0].disposition == "inline" and p.attachments[0].content_id == "img1"


def test_parse_legacy_encoding_and_missing_headers():
    raw = (b"From: =?koi8-r?B?4NDQyQ==?= <k@r.ru>\r\nTo: x@y.ru\r\nSubject: =?koi8-r?B?8NLJ18XU?=\r\n"
           b"Content-Type: text/plain; charset=koi8-r\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
           + "Привет".encode("koi8-r"))
    p = parse_mime(raw)
    assert p.subject == "Привет" and p.text == "Привет"
    assert p.message_id is None and p.references == [] and p.date is None


def test_html_to_text():
    assert html_to_text("<p>a&amp;b</p><br><div>c</div>") == "a&b\nc"
