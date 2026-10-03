"""Эталонные диалоги `evals/dialogs/*.yaml` (формат — evals/README.md)."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.contracts import UserInput

# apps/api/evals/dialogs.py → корень репозитория на 3 уровня выше.
REPO_ROOT = Path(__file__).resolve().parents[3]
DIALOGS_DIR = REPO_ROOT / "evals" / "dialogs"


class InvalidDialogError(Exception):
    """Файл диалога не разбирается или не соответствует формату."""


class Expect(BaseModel):
    """Ожидания хода. `clarifies`, `max_questions`, `judge` проверяет LLM-судья."""

    model_config = ConfigDict(extra="forbid")

    clarifies: bool | None = None
    max_questions: int | None = Field(default=None, ge=0)
    tools_called: list[str] = Field(default_factory=list)
    tools_not_called: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    state_contains: dict[str, Any] = Field(default_factory=dict)
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    judge: str | None = None


class DialogTurn(BaseModel):
    """Ход пользователя: текст (`user`) или UserInput (`input`) — ровно одно из двух."""

    model_config = ConfigDict(extra="forbid")

    user: str | None = Field(default=None, min_length=1)
    input: UserInput | None = None
    expect: Expect = Field(default_factory=Expect)

    @model_validator(mode="after")
    def _one_input(self) -> Self:
        if (self.user is None) == (self.input is None):
            raise ValueError("в ходе нужно ровно одно из `user` и `input`")
        return self

    def user_input(self) -> dict[str, Any]:
        """UserInput хода в JSON — как его присылает клиент."""
        if self.input is not None:
            data: dict[str, Any] = self.input.root.model_dump(mode="json")
            return data
        return {"type": "text", "text": self.user}


class Dialog(BaseModel):
    """`tenant` — slug тенанта: конфиг берётся из `config/tenants/<tenant>.yaml`."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    tenant: str = Field(min_length=1)
    scenario: str | None = None
    tags: list[str] = Field(default_factory=list)
    turns: list[DialogTurn] = Field(min_length=1)


def load_dialog(path: Path) -> Dialog:
    try:
        return Dialog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError) as e:
        raise InvalidDialogError(f"{path.name}: {e}") from e


def load_dialogs(directory: Path = DIALOGS_DIR) -> list[Dialog]:
    """Все диалоги каталога в порядке имён файлов; id не должны повторяться."""
    dialogs = [load_dialog(path) for path in sorted(directory.glob("*.yaml"))]
    seen: set[str] = set()
    for dialog in dialogs:
        if dialog.id in seen:
            raise InvalidDialogError(f"повторяется id диалога {dialog.id!r}")
        seen.add(dialog.id)
    return dialogs


def select_dialogs(dialogs: Iterable[Dialog], pattern: str | None) -> list[Dialog]:
    """Фильтр `make eval f=...`: подстрока id или точное совпадение с тегом."""
    if not pattern:
        return list(dialogs)
    return [d for d in dialogs if pattern in d.id or pattern in d.tags]
