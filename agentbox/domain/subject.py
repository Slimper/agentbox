import re

_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw|wg|sv|vs|ответ|пересылка)\s*(\[\d+\])?\s*:\s*", re.IGNORECASE)
_WS = re.compile(r"\s+")


def strip_reply_prefixes(subject: str | None) -> str:
    s = subject or ""
    while True:
        new = _PREFIX.sub("", s, count=1)
        if new == s:
            break
        s = new
    return s.strip()


def normalize_subject(subject: str | None) -> str:
    return _WS.sub(" ", strip_reply_prefixes(subject)).strip().lower()
