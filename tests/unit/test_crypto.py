import pytest
from cryptography.fernet import InvalidToken

from agentbox.security.crypto import decrypt_json, decrypt_str, encrypt_json, encrypt_str


def test_roundtrip_str_and_json():
    key = "any-string-works-as-key"
    assert decrypt_str(key, encrypt_str(key, "s3cret")) == "s3cret"
    assert decrypt_json(key, encrypt_json(key, {"host": "h", "port": 25})) == {"host": "h", "port": 25}


def test_different_key_fails():
    token = encrypt_str("k1", "x")
    with pytest.raises(InvalidToken):
        decrypt_str("k2", token)
