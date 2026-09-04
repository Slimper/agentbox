import base64
import hashlib
import json
from functools import lru_cache

from cryptography.fernet import Fernet


@lru_cache(maxsize=8)
def _fernet(key: str) -> Fernet:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_str(key: str, value: str) -> str:
    return _fernet(key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(key: str, token: str) -> str:
    return _fernet(key).decrypt(token.encode("ascii")).decode("utf-8")


def encrypt_json(key: str, value: dict) -> str:
    return encrypt_str(key, json.dumps(value, separators=(",", ":"), sort_keys=True))


def decrypt_json(key: str, token: str) -> dict:
    return json.loads(decrypt_str(key, token))
