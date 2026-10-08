"""Структура модулей по architecture.md §3. Границы импортов проверяет import-linter (P1-03)."""

from pathlib import Path

import pytest

import app.modules

MODULES = [
    "access",
    "tenants",
    "chat",
    "agent",
    "tools",
    "knowledge",
    "leads",
    "memory",
    "observability",
    "shared",
]
LAYERS = ["domain", "application", "infrastructure", "api"]
MODULES_DIR = Path(app.modules.__file__).parent


@pytest.mark.parametrize("module", MODULES)
def test_module_has_layers_and_public(module: str) -> None:
    root = MODULES_DIR / module

    assert (root / "public.py").is_file()
    for layer in LAYERS:
        assert (root / layer / "__init__.py").is_file(), f"{module}/{layer}"


def test_no_unexpected_modules() -> None:
    found = {p.name for p in MODULES_DIR.iterdir() if p.is_dir() and p.name != "__pycache__"}

    assert found == set(MODULES)
