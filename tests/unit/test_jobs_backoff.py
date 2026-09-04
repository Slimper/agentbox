from agentbox.jobs.queue import backoff_for, max_attempts_for


def test_backoff_and_max_attempts():
    assert max_attempts_for("outbound_send") == 6
    assert backoff_for("outbound_send", 1) == 30
    assert backoff_for("outbound_send", 5) == 14400
    assert backoff_for("outbound_send", 99) == 14400
    assert max_attempts_for("webhook_deliver") == 8
    assert backoff_for("inbox_expire", 1) == 60
