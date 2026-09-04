import hashlib
import hmac
import time


def verify_webhook_signature(secret: str, header: str, body: bytes, tolerance: int = 300,
                             now: int | None = None) -> bool:
    """Verify an `AgentBox-Signature: t=<unix>,v1=<hex>` header against the raw request body."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    now = int(time.time()) if now is None else now
    if abs(now - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in parts.items() if k == "v1")
