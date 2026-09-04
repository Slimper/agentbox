from agentbox.webhooks.signing import sign, signature_header, verify_signature


def test_sign_and_verify():
    body = b'{"id":"evt_1"}'
    header = signature_header("whsec_x", body, 1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert verify_signature("whsec_x", header, body, now=1_700_000_100)
    assert not verify_signature("whsec_y", header, body, now=1_700_000_100)
    assert not verify_signature("whsec_x", header, body + b" ", now=1_700_000_100)
    assert not verify_signature("whsec_x", header, body, now=1_700_001_000)
    assert sign("s", b"b", 1) == sign("s", b"b", 1)
