"""Seed источников тенанта из декларации в YAML (ADR-0019): разбор, проверка, upsert по имени,
копирование файлов в каталог источника.

Seed не удаляет источники, которых нет в декларации (их данные и история синхронизаций
остаются), — только сообщает о них. Синхронизацию seed не запускает: это `app.cli sync`.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.domain.errors import InvalidSourceDeclarationError
from app.modules.knowledge.domain.ingestion import SourceSpec
from app.modules.knowledge.domain.ports import SourceFileStore, SourceRegistry
from app.modules.knowledge.domain.source_seed import (
    SeedAction,
    SeededSource,
    SeedSourcesResult,
    SourceDeclaration,
)
from app.modules.shared.kernel import TenantId

# Проверка конфига моделью коннектора; невалидный — ValueError с текстом.
type ConfigValidator = Callable[[SourceKind, dict[str, Any]], None]

_FILE_KINDS = frozenset({SourceKind.FILE, SourceKind.TABLE})


class _Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=64)
    kind: SourceKind
    config: dict[str, Any] = Field(default_factory=dict)
    files: str | None = None


def parse_source_declarations(
    raw: Sequence[Mapping[str, Any]], base_dir: Path
) -> list[SourceDeclaration]:
    """Секция `sources` YAML тенанта; относительный `files` — от каталога YAML-файла."""
    result: list[SourceDeclaration] = []
    for index, entry in enumerate(raw, start=1):
        try:
            item = _Declaration.model_validate(entry)
        except ValidationError as e:
            raise InvalidSourceDeclarationError(f"sources[{index}]: {e}") from e
        where = f"источник {item.name!r}"
        if any(d.name == item.name for d in result):
            raise InvalidSourceDeclarationError(f"{where}: имя повторяется")
        files: Path | None = None
        if item.files is not None:
            if item.kind not in _FILE_KINDS:
                raise InvalidSourceDeclarationError(f"{where}: files — только для file и table")
            files = (base_dir / item.files).resolve()
            if not files.is_dir():
                raise InvalidSourceDeclarationError(f"{where}: каталог files не найден: {files}")
        result.append(SourceDeclaration(item.name, item.kind, item.config, files))
    return result


async def seed_sources(
    tenant_id: TenantId,
    declarations: Sequence[SourceDeclaration],
    *,
    registry: SourceRegistry,
    files: SourceFileStore,
    validate: ConfigValidator,
) -> SeedSourcesResult:
    """Идемпотентно: повтор с той же декларацией ничего не меняет (кроме копирования
    изменившихся файлов). Все конфиги проверяются до первой записи."""
    for declaration in declarations:
        try:
            validate(declaration.kind, declaration.config)
        except ValueError as e:
            raise InvalidSourceDeclarationError(f"источник {declaration.name!r}: {e}") from e

    seeded: list[SeededSource] = []
    for declaration in declarations:
        existing = await registry.get_by_name(tenant_id, declaration.name)
        if existing is None:
            source_id = await registry.create(
                tenant_id, declaration.name, declaration.kind, declaration.config
            )
            action = SeedAction.CREATED
        elif existing.kind is not declaration.kind:
            # Документы и сущности источника — данные его коннектора: смена вида их не перенесёт.
            raise InvalidSourceDeclarationError(
                f"источник {declaration.name!r}: вид {existing.kind} → {declaration.kind} "
                "не меняется — объявите источник с другим именем"
            )
        elif existing.config != declaration.config:
            source_id = existing.id
            await registry.update_config(tenant_id, source_id, declaration.config)
            action = SeedAction.UPDATED
        else:
            source_id, action = existing.id, SeedAction.UNCHANGED

        mirrored = None
        if declaration.files is not None:
            spec = SourceSpec(tenant_id, source_id, declaration.config)
            mirrored = await asyncio.to_thread(files.mirror, spec, declaration.files)
        seeded.append(SeededSource(declaration.name, source_id, action, mirrored))

    declared = {d.name for d in declarations}
    undeclared = [n for n in await registry.names(tenant_id) if n not in declared]
    return SeedSourcesResult(seeded, sorted(undeclared))
