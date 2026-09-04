import hashlib
import hmac
import time


def sign(secret: str, body: bytes, timestamp: int) -> str:
    return hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def signature_header(secret: str, body: bytes, timestamp: int) -> str:
    return f"t={timestamp},v1={sign(secret, body, timestamp)}"


def verify_signature(secret: str, header: str, body: bytes, tolerance: int = 300, now: int | None = None) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    now = int(time.time()) if now is None else now
    if abs(now - ts) > tolerance:
        return False
    expected = sign(secret, body, ts)
    return any(hmac.compare_digest(expected, v) for k, v in parts.items() if k == "v1")
