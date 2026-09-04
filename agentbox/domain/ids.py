from ulid import ULID

PREFIXES = frozenset(
    {"org", "key", "dom", "ibx", "thr", "msg", "att", "evt", "whk", "wdl", "dat", "pa", "ing", "pol", "sup", "rr",
     "usr", "mem", "ses", "tok", "inv", "lead", "sso", "mbc"}
)


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix}")
    return f"{prefix}_{ULID()}"
