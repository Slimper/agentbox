from datetime import timedelta

import pytest

from agentbox.domain.ttl import parse_ttl


@pytest.mark.parametrize("s,td", [("30s", timedelta(seconds=30)), ("15m", timedelta(minutes=15)),
                                  ("24h", timedelta(hours=24)), ("7d", timedelta(days=7))])
def test_parse_ttl(s, td):
    assert parse_ttl(s) == td


@pytest.mark.parametrize("bad", ["", "abc", "10", "31d", "0h", "-1h", "1w"])
def test_parse_ttl_rejects(bad):
    with pytest.raises(ValueError):
        parse_ttl(bad)
