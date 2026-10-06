"""ConversationSummarizer поверх HistorySummarizer (модуль agent): модель сводки из AgentConfig
диалога, сообщения — в том виде, в каком их видит модель хода."""

from collections.abc import Sequence
from typing import Any

from app.contracts import AgentConfig
from app.modules.agent.public import HistorySummarizer
from app.modules.chat.domain.entities import ChatMessage
from app.modules.chat.infrastructure.loop_agent import LLMForProvider, to_llm_messages


class LoopConversationSummarizer:
    """`llm_for` — клиент по провайдеру, как у LoopTurnAgent."""

    def __init__(self, llm_for: LLMForProvider) -> None:
        self._llm_for = llm_for

    async def summarize(
        self, agent_config: dict[str, Any], previous: str | None, messages: Sequence[ChatMessage]
    ) -> str | None:
        config = AgentConfig.model_validate(agent_config)
        model = (config.memory and config.memory.summary_model) or config.model.primary
        return await HistorySummarizer(self._llm_for(model.provider)).summarize(
            model.name, previous, tuple(to_llm_messages(messages)), temperature=model.temperature
        )
