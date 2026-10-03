"""Параметры сэмплинга — подсказка, которую применяет адаптер провайдера (ADR-0009)."""

import structlog

logger = structlog.get_logger(__name__)


def log_dropped_param(provider: str, model: str, param: str) -> None:
    """Адаптер не передал заданный параметр: видно в трейсе хода, но не шумит.
    tenant_id и conversation_id приходят из контекста structlog."""
    logger.debug("llm.param_dropped", provider=provider, model=model, param=param)
