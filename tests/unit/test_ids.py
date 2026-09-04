import pytest

from agentbox.domain.ids import new_id


def test_new_id_has_prefix_and_is_sortable():
    a = new_id("ibx")
    b = new_id("ibx")
    assert a.startswith("ibx_") and len(a) == 4 + 26
    assert a < b


def test_unknown_prefix_rejected():
    with pytest.raises(ValueError):
        new_id("zzz")
