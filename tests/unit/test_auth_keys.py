from agentbox.api.auth import Principal, generate_api_key, hash_api_key


def test_generate_api_key_shape():
    plaintext, prefix, digest = generate_api_key("live")
    assert plaintext.startswith("ab_live_") and len(plaintext) > 30
    assert prefix == plaintext[:12]
    assert digest == hash_api_key(plaintext) and len(digest) == 64


def test_principal_scopes():
    p = Principal("org_1", "key_1", frozenset({"inboxes:read"}), "live")
    assert p.has("inboxes:read") and not p.has("inboxes:write")
    assert Principal("o", "k", frozenset({"admin"}), "live").has("anything")
