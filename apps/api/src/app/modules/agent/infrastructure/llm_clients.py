"""Выбор LLM-клиента по провайдеру из AgentConfig (`model.primary.provider`).

Клиенты создаются при первом обращении и переиспользуются: у каждого свой пул HTTP-соединений.
Ключи — из окружения (Settings); их передаёт тот, кто собирает приложение.
"""

from collections.abc import Callable
from typing import Literal

from app.modules.agent.domain.llm import LLMClient, LLMError
from app.modules.agent.infrastructure.anthropic_llm import create_anthropic_llm
from app.modules.agent.infrastructure.openai_llm import create_openai_llm

type Provider = Literal["openai", "anthropic"]


class LLMClients:
    def __init__(
        self,
        *,
        openai_api_key: str | None = None,
        openai_base_url: str | None = None,
        anthropic_api_key: str | None = None,
        anthropic_base_url: str | None = None,
    ) -> None:
        self._factories: dict[Provider, Callable[[], LLMClient] | None] = {
            "openai": (
                (lambda: create_openai_llm(openai_api_key, base_url=openai_base_url))
                if openai_api_key
                else None
            ),
            "anthropic": (
                (lambda: create_anthropic_llm(anthropic_api_key, base_url=anthropic_base_url))
                if anthropic_api_key
                else None
            ),
        }
        self._clients: dict[Provider, LLMClient] = {}

    def for_provider(self, provider: Provider) -> LLMClient:
        """Провайдер без ключа — LLMError без ретрая: ошибка конфигурации, повтор не поможет."""
        if (client := self._clients.get(provider)) is not None:
            return client
        factory = self._factories.get(provider)
        if factory is None:
            raise LLMError(f"провайдер {provider!r} не настроен: нет API-ключа", retryable=False)
        client = self._clients[provider] = factory()
        return client
