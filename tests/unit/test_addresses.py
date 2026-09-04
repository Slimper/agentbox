import pytest

from agentbox.domain.addresses import (
    generate_username,
    is_valid_email,
    normalize_email,
    split_address,
    validate_username,
)


def test_validate_username_accepts_and_normalizes():
    assert validate_username("Procurement-Agent.1") == "procurement-agent.1"


@pytest.mark.parametrize("bad", ["admin", "Postmaster", "", "a" * 65, "-x", "x..y", "x y", "x@y"])
def test_validate_username_rejects(bad):
    with pytest.raises(ValueError):
        validate_username(bad)


def test_generate_username_shape():
    u = generate_username()
    assert validate_username(u) == u
    assert "-" in u


def test_email_helpers():
    assert is_valid_email("Sales@Supplier.ru")
    assert not is_valid_email("nope")
    assert not is_valid_email("a@b")
    assert normalize_email("  Sales@Supplier.RU ") == "sales@supplier.ru"
    assert split_address("x@y.z") == ("x", "y.z")
