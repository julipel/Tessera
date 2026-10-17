"""`app.cli seed`: ключи виджета по slug (key_hash уникален — один ключ на всех не годится),
флаг --force-config."""

from typing import Any

import pytest

from app import cli
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


@pytest.mark.parametrize(("argv", "force"), [([], False), (["--force-config"], True)])
def test_force_config_flag_reaches_seed(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], force: bool
) -> None:
    """Без флага seed не затирает конфиг из админки (REVIEW.md: R-1)."""
    calls: list[bool] = []

    async def fake_seed(*_: Any, force_config: bool) -> None:
        calls.append(force_config)

    monkeypatch.setattr(cli, "seed", fake_seed)
    assert cli.main(["seed", *argv]) == 0
    assert calls == [force]
