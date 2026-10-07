"""Публичный интерфейс модуля observability — единственная точка входа для других модулей."""

from app.modules.observability.api.router import router as admin_router
from app.modules.observability.domain.events import AgentEventEntry
from app.modules.observability.infrastructure.models import AgentEventRecord
from app.modules.observability.infrastructure.repositories import AgentEventRepository

__all__ = ["AgentEventEntry", "AgentEventRecord", "AgentEventRepository", "admin_router"]
