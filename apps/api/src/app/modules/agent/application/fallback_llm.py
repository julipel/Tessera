"""Ретраи шага модели и резервная модель (architecture.md §12, ADR-0027).

HTTP-ретраи с backoff (429, 5xx, таймауты до начала стрима) делает SDK провайдера внутри
каждой попытки. Здесь — повтор шага целиком и переход на резервную модель, пока клиенту
ничего не отдано: после первого чанка повтор задублировал бы ответ, ошибка пробрасывается.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace

import structlog

from app.modules.agent.domain.llm import (
    AssistantMessage,
    LLMChunk,
    LLMClient,
    LLMError,
    LLMMessage,
    LLMRequest,
)

logger = structlog.get_logger(__name__)

PRIMARY_ATTEMPTS = 2
RETRY_DELAY_S = 1.0


@dataclass(frozen=True, slots=True)
class FallbackModel:
    """Резервная модель: `client` создаёт клиент провайдера при первом переходе (провайдер
    без ключа — `LLMError`), `model` и `temperature` заменяют основные в запросе."""

    client: Callable[[], LLMClient]
    model: str
    temperature: float | None = None


class FallbackLLM:
    """`LLMClient` хода: основная модель — до `PRIMARY_ATTEMPTS` попыток с паузой
    `RETRY_DELAY_S`, затем одна попытка резервной. Повторяется только `retryable` ошибка,
    пока шаг ничего не отдал. После перехода на резервную модель следующие шаги хода идут
    сразу в неё; `provider_items` основной модели из запроса убираются (ADR-0010)."""

    def __init__(
        self,
        primary: LLMClient,
        fallback: FallbackModel | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_client: LLMClient | None = None
        self._sleep = sleep

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        if self._fallback_client is None:
            error: LLMError | None = None
            for attempt in range(1, PRIMARY_ATTEMPTS + 1):
                emitted = False
                try:
                    async for chunk in self._primary.stream(request):
                        emitted = True
                        yield chunk
                    return
                except LLMError as e:
                    if emitted or not e.retryable:
                        raise
                    error = e
                if attempt < PRIMARY_ATTEMPTS:
                    logger.warning(
                        "llm.retry", model=request.model, attempt=attempt, error=str(error)
                    )
                    await self._sleep(RETRY_DELAY_S * attempt)
            assert error is not None
            self._fallback_client = self._switch(request, error)
        assert self._fallback is not None
        fallback_request = replace(
            request,
            model=self._fallback.model,
            temperature=self._fallback.temperature,
            messages=tuple(_without_provider_items(m) for m in request.messages),
        )
        async for chunk in self._fallback_client.stream(fallback_request):
            yield chunk

    def _switch(self, request: LLMRequest, error: LLMError) -> LLMClient:
        """Клиент резервной модели; без неё или без ключа — исходная ошибка основной."""
        if self._fallback is None:
            raise error
        try:
            client = self._fallback.client()
        except LLMError as e:
            logger.error("llm.fallback_unavailable", model=self._fallback.model, error=str(e))
            raise error from e
        logger.warning(
            "llm.fallback", model=request.model, fallback=self._fallback.model, error=str(error)
        )
        return client


def _without_provider_items(message: LLMMessage) -> LLMMessage:
    if isinstance(message, AssistantMessage) and message.provider_items:
        return replace(message, provider_items=())
    return message
