import pytest
from pydantic import ValidationError

from agentbox.api.auth import _WINDOWS, check_rate_limit
from agentbox.governance.policies import DEFAULT_POLICY, merge_policy, validate_policy_config


def test_validate_and_merge():
    cfg = validate_policy_config({"recipient_policy": {"allowed_domains": ["@Supplier.RU", "x.com "]},
                                  "limits": {"emails_per_day": 5}})
    assert cfg == {"recipient_policy": {"allowed_domains": ["supplier.ru", "x.com"]}, "limits": {"emails_per_day": 5}}
    eff = merge_policy(DEFAULT_POLICY, cfg)
    assert eff["limits"]["emails_per_day"] == 5 and eff["limits"]["emails_per_hour"] == 500
    assert eff["recipient_policy"]["blocked_domains"] == []
    with pytest.raises(ValidationError):
        validate_policy_config({"nope": 1})
    with pytest.raises(ValidationError):
        validate_policy_config({"limits": {"emails_per_day": -1}})


def test_rate_limit_window():
    _WINDOWS.clear()
    assert check_rate_limit("k", 2) is None
    assert check_rate_limit("k", 2) is None
    assert isinstance(check_rate_limit("k", 2), int)
    assert check_rate_limit("other", 2) is None
    assert check_rate_limit("k", 0) is None
