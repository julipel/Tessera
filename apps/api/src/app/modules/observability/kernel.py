"""Типы observability без фреймворков — для domain-слоёв других модулей (ADR-0008)."""

from app.modules.observability.domain.events import AgentEventEntry

__all__ = ["AgentEventEntry"]
