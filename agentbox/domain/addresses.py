import re
import secrets

RESERVED_LOCAL_PARTS = frozenset(
    {"admin", "postmaster", "abuse", "support", "security", "billing", "root", "mailer-daemon",
     "noreply", "no-reply"}
)

_USERNAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_EMAIL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")

_WORDS = [
    "amber", "birch", "cedar", "delta", "ember", "falcon", "granite", "harbor", "indigo", "juniper",
    "kestrel", "lumen", "maple", "nimbus", "orbit", "pebble", "quartz", "raven", "summit", "tundra",
]


def validate_username(username: str) -> str:
    u = (username or "").strip().lower()
    if not u or ".." in u or not _USERNAME.match(u):
        raise ValueError("username must be 1-64 chars of a-z 0-9 . _ - and start/end alphanumeric")
    if u in RESERVED_LOCAL_PARTS:
        raise ValueError(f"username '{u}' is reserved")
    return u


def generate_username() -> str:
    return f"{secrets.choice(_WORDS)}-{secrets.token_hex(2)}"


def is_valid_email(value: str) -> bool:
    v = (value or "").strip()
    return bool(_EMAIL.match(v)) and len(v) <= 254


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def split_address(value: str) -> tuple[str, str]:
    local, _, domain = normalize_email(value).rpartition("@")
    return local, domain
