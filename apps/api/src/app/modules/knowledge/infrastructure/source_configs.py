"""Проверка `Source.config` моделью конфига коннектора — до записи источника (ADR-0019)."""

from typing import Any

from pydantic import BaseModel, ValidationError

from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.infrastructure.database_connector import DatabaseSourceConfig
from app.modules.knowledge.infrastructure.http_api_connector import HttpApiSourceConfig
from app.modules.knowledge.infrastructure.table_connector import TableSourceConfig
from app.modules.knowledge.infrastructure.website_connector import WebsiteSourceConfig

_MODELS: dict[SourceKind, type[BaseModel]] = {
    SourceKind.TABLE: TableSourceConfig,
    SourceKind.WEBSITE: WebsiteSourceConfig,
    SourceKind.HTTP_API: HttpApiSourceConfig,
    SourceKind.DATABASE: DatabaseSourceConfig,
}


def validate_source_config(kind: SourceKind, config: dict[str, Any]) -> None:
    """Невалидный конфиг — ValueError. Для `file` конфиг пустой: каталог задаёт платформа."""
    model = _MODELS.get(kind)
    if model is None:
        if config:
            raise ValueError(f"у источника {kind} нет настроек, config должен быть пустым")
        return
    try:
        model.model_validate(config)
    except ValidationError as e:
        raise ValueError(f"неверный конфиг источника {kind}: {e}") from None
