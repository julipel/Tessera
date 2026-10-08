"""Источники знаний и статусы синхронизации (architecture.md §8, §11)."""

from enum import StrEnum


class SourceKind(StrEnum):
    """Тип коннектора источника."""

    WEBSITE = "website"
    FILE = "file"
    TABLE = "table"
    HTTP_API = "http_api"
    DATABASE = "database"


class SourceOrigin(StrEnum):
    """Где объявлен источник: в YAML тенанта (меняет только `make seed`) или в админке
    (ADR-0037)."""

    YAML = "yaml"
    ADMIN = "admin"


class SourceStatus(StrEnum):
    """`paused` — источник не синхронизируется по расписанию, данные остаются в поиске."""

    ACTIVE = "active"
    PAUSED = "paused"


class SyncStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
