"""`app.cli seed`: ключи виджета по slug (key_hash уникален — один ключ на всех не годится)."""

import pytest

from app.cli import widget_keys_by_slug


def test_no_value_means_generated_keys() -> None:
    assert widget_keys_by_slug(None, ["a", "b"]) == {}
    assert widget_keys_by_slug("", ["a"]) == {}


def test_plain_key_for_single_tenant() -> None:
    assert widget_keys_by_slug("wk_a", ["a"]) == {"a": "wk_a"}


def test_plain_key_for_several_tenants_is_rejected() -> None:
    with pytest.raises(ValueError, match="slug=key"):
        widget_keys_by_slug("wk_a", ["a", "b"])


def test_keys_by_slug_skip_tenants_not_being_seeded() -> None:
    keys = widget_keys_by_slug(" a = wk_a , b=wk_b,c=wk_c", ["a", "b"])

    assert keys == {"a": "wk_a", "b": "wk_b"}


@pytest.mark.parametrize("value", ["a=wk_a,wk_b", "a=", "=wk_a"])
def test_malformed_pairs_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="slug=key"):
        widget_keys_by_slug(value, ["a"])
