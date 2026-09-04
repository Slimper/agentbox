from __future__ import annotations

import email.policy
import email.utils
from dataclasses import dataclass, field
from datetime import datetime
from email.headerregistry import Address as HdrAddress
from email.message import EmailMessage

from agentbox.mime.parse import Address


@dataclass
class OutboundAttachment:
    filename: str
    content_type: str
    content: bytes
    disposition: str = "attachment"
    content_id: str | None = None


@dataclass
class OutboundMessage:
    message_id: str
    from_: Address
    to: list[Address]
    cc: list[Address]
    bcc: list[Address]
    reply_to: list[Address]
    subject: str
    text: str | None
    html: str | None
    in_reply_to: str | None
    references: list[str]
    headers: list[list[str]]
    attachments: list[OutboundAttachment] = field(default_factory=list)
    date: datetime | None = None


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\r", " ").replace("\n", " ").split())


def format_address(a: Address) -> str:
    return str(HdrAddress(display_name=_clean(a.name), addr_spec=a.email))


def _split_type(content_type: str) -> tuple[str, str]:
    main, _, sub = (content_type or "application/octet-stream").partition("/")
    return (main or "application", sub or "octet-stream")


def build_mime(m: OutboundMessage) -> bytes:
    msg = EmailMessage()
    msg["From"] = format_address(m.from_)
    if m.to:
        msg["To"] = ", ".join(format_address(a) for a in m.to)
    if m.cc:
        msg["Cc"] = ", ".join(format_address(a) for a in m.cc)
    if m.reply_to:
        msg["Reply-To"] = ", ".join(format_address(a) for a in m.reply_to)
    msg["Subject"] = _clean(m.subject)
    msg["Date"] = email.utils.format_datetime(m.date or email.utils.localtime())
    msg["Message-ID"] = m.message_id
    if m.in_reply_to:
        msg["In-Reply-To"] = m.in_reply_to
    if m.references:
        msg["References"] = " ".join(m.references)
    for name, value in m.headers:
        msg[_clean(name)] = _clean(value)

    if m.text is not None and m.html is not None:
        msg.set_content(m.text)
        msg.add_alternative(m.html, subtype="html")
        html_part = msg.get_payload()[-1]
    elif m.html is not None:
        msg.set_content(m.html, subtype="html")
        html_part = msg
    else:
        msg.set_content(m.text or "")
        html_part = None

    for a in m.attachments:
        main, sub = _split_type(a.content_type)
        if a.disposition == "inline" and a.content_id and html_part is not None:
            html_part.add_related(a.content, maintype=main, subtype=sub, cid=f"<{a.content_id}>",
                                  filename=a.filename, disposition="inline")
    for a in m.attachments:
        main, sub = _split_type(a.content_type)
        if a.disposition == "inline" and a.content_id and html_part is not None:
            continue
        msg.add_attachment(a.content, maintype=main, subtype=sub, filename=a.filename)
    return msg.as_bytes(policy=email.policy.SMTP)
