from agentbox.api.idempotency import request_hash


def test_hash_ignores_key_order_and_whitespace():
    assert request_hash(b'{"a":1,"b":2}') == request_hash(b'{ "b": 2, "a": 1 }')
    assert request_hash(b'{"a":1}') != request_hash(b'{"a":2}')
    assert request_hash(b"not json") == request_hash(b"not json")
