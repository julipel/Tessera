"""Источники тенанта из декларации (YAML тенанта, ADR-0019): что объявлено и что сделал seed."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from app.modules.knowledge.domain.entities import SourceKind, SourceOrigin


@dataclass(frozen=True, slots=True)
class SourceDeclaration:
    """`name` — ключ источника в пределах тенанта; `files` — каталог, содержимое которого
    seed копирует в каталог источника (только `file`/`table`)."""

    name: str
    kind: SourceKind
    config: dict[str, Any]
    files: Path | None = None


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    id: UUID
    kind: SourceKind
    config: dict[str, Any]
    origin: SourceOrigin = SourceOrigin.YAML


class SeedAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class MirrorStats:
    copied: int = 0
    removed: int = 0
    unchanged: int = 0


@dataclass(frozen=True, slots=True)
class SeededSource:
    name: str
    source_id: UUID
    action: SeedAction
    files: MirrorStats | None = None


@dataclass(frozen=True, slots=True)
class SeedSourcesResult:
    seeded: list[SeededSource]
    # Источники тенанта с именем, которых нет в декларации: seed их не удаляет.
    undeclared: list[str]
