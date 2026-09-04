from __future__ import annotations

import email
import email.policy
import email.utils
import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage

_MSGID = re.compile(r"<[^<>\s]+>")


@dataclass(frozen=True)
class Address:
    email: str
    name: str | None = None

    def to_dict(self) -> dict:
        return {"email": self.email, "name": self.name}


@dataclass
class ParsedPart:
    filename: str
    content_type: str
    content: bytes
    disposition: str = "attachment"
    content_id: str | None = None


@dataclass
class ParsedMessage:
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    subject: str
    from_: list[Address]
    to: list[Address]
    cc: list[Address]
    reply_to: list[Address]
    date: datetime | None
    text: str | None
    html: str | None
    headers: list[tuple[str, str]] = field(default_factory=list)
    attachments: list[ParsedPart] = field(default_factory=list)
    size_bytes: int = 0


def _safe_str(value) -> str:
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return repr(value)


def _addresses(msg: EmailMessage, name: str) -> list[Address]:
    out: list[Address] = []
    for value in msg.get_all(name, []):
        try:
            for a in value.addresses:
                if a.addr_spec and "@" in a.addr_spec:
                    out.append(Address(a.addr_spec.lower(), a.display_name or None))
            continue
        except Exception:  # noqa: BLE001
            pass
        for n, e in email.utils.getaddresses([_safe_str(value)]):
            if e and "@" in e:
                out.append(Address(e.lower(), n or None))
    return out


def _message_ids(value: str | None) -> list[str]:
    return _MSGID.findall(value or "")


def _content_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


_TAG_STRIP = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK = re.compile(r"</?(p|div|br|tr|li|h[1-6]|blockquote)[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    s = _TAG_STRIP.sub("", html)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub("", s)
    s = html_lib.unescape(s)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in s.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_mime(raw: bytes) -> ParsedMessage:
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
    headers: list[tuple[str, str]] = []
    for key in msg.keys():
        for value in msg.get_all(key, []):
            headers.append((key, _safe_str(value)))

    text_part = html_part = None
    try:
        text_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
    except Exception:  # noqa: BLE001
        pass
    text = _content_text(text_part) if text_part is not None else None
    html = _content_text(html_part) if html_part is not None else None
    if text is None and html is not None:
        text = html_to_text(html)

    body_ids = {id(p) for p in (text_part, html_part) if p is not None}
    attachments: list[ParsedPart] = []
    n = 0
    for part in msg.walk():
        if part.is_multipart() or id(part) in body_ids:
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        filename = part.get_filename()
        if ctype in ("text/plain", "text/html") and disp in (None, "inline") and not filename:
            continue
        if ctype == "message/delivery-status":
            continue
        n += 1
        content = part.get_payload(decode=True) or b""
        cid = (part.get("Content-ID") or "").strip().strip("<>") or None
        referenced = bool(cid) and f"cid:{cid}" in (html or "")
        disposition = "inline" if (disp == "inline" or (cid and (disp is None or referenced))) else "attachment"
        ext = ctype.split("/")[-1].split("+")[0] if "/" in ctype else "bin"
        attachments.append(ParsedPart(filename=filename or f"part-{n}.{ext}", content_type=ctype,
                                      content=content, disposition=disposition, content_id=cid))

    date = None
    if msg.get("Date"):
        try:
            date = email.utils.parsedate_to_datetime(_safe_str(msg["Date"]))
        except (TypeError, ValueError):
            date = None

    mids = _message_ids(_safe_str(msg.get("Message-ID")) if msg.get("Message-ID") else None)
    irt = _message_ids(_safe_str(msg.get("In-Reply-To")) if msg.get("In-Reply-To") else None)
    refs = _message_ids(" ".join(_safe_str(v) for v in msg.get_all("References", [])))
    return ParsedMessage(
        message_id=mids[0] if mids else None, in_reply_to=irt[0] if irt else None, references=refs,
        subject=_safe_str(msg.get("Subject", "")).strip(), from_=_addresses(msg, "From"),
        to=_addresses(msg, "To"), cc=_addresses(msg, "Cc"), reply_to=_addresses(msg, "Reply-To"),
        date=date, text=text, html=html, headers=headers, attachments=attachments, size_bytes=len(raw),
    )
