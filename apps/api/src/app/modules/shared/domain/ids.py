"""Идентификаторы, общие для всех модулей."""

from typing import NewType
from uuid import UUID

# Отдельный тип, чтобы tenant_id нельзя было случайно перепутать с id сущности.
TenantId = NewType("TenantId", UUID)
