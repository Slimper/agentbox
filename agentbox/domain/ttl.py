import re
from datetime import timedelta

_TTL = re.compile(r"^(\d+)([smhd])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_TTL = timedelta(days=30)


def parse_ttl(value: str) -> timedelta:
    m = _TTL.match((value or "").strip())
    if not m:
        raise ValueError("ttl must look like 30s, 15m, 24h or 7d")
    seconds = int(m.group(1)) * _UNIT[m.group(2)]
    if seconds <= 0:
        raise ValueError("ttl must be positive")
    td = timedelta(seconds=seconds)
    if td > MAX_TTL:
        raise ValueError("ttl must be at most 30d")
    return td
