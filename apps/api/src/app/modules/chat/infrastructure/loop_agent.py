"""TurnAgent поверх агентного цикла (модуль agent): AgentConfig и история → TurnContext.

В БД не ходит: конфиг и история приходят в TurnRequest (их загружает start_turn).
"""

import json
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from app.contracts import AgentConfig
from app.modules.agent.public import (
    AgentEvent,
    AgentLoop,
    AssistantMessage,
    LLMClient,
    LLMMessage,
    RuntimeContext,
    ToolExecutor,
    TurnContext,
    TurnLimits,
    UserMessage,
    build_system_prompt,
)
from app.modules.chat.domain.entities import ChatMessage, MessageRole, TurnRequest

type LLMForProvider = Callable[[Literal["openai", "anthropic"]], LLMClient]


class LoopTurnAgent:
    """`llm_for` — клиент по провайдеру из AgentConfig (в приложении — `LLMClients.for_provider`),
    `now` — часы для Runtime-слоя промпта (в тестах фиксированные)."""

    def __init__(
        self,
        llm_for: LLMForProvider,
        tools: ToolExecutor,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._llm_for = llm_for
        self._tools = tools
        self._now = now

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        config = AgentConfig.model_validate(request.agent_config)
        primary = config.model.primary
        limits = config.limits
        prompt = build_system_prompt(config, RuntimeContext(now=self._now()))
        ctx = TurnContext(
            tenant_id=request.tenant_id,
            conversation_id=request.conversation_id,
            turn_id=request.turn_id,
            model=primary.name,
            system=prompt.text,
            history=tuple(to_llm_messages(request.history)),
            limits=TurnLimits(
                # None в конфиге невозможен по смыслу; значения — как default в схеме.
                max_steps=limits.max_steps or 6,
                max_tool_retries=2 if limits.max_tool_retries is None else limits.max_tool_retries,
            ),
            fallback_message=config.assistant.fallback_message,
            # ADR-0009: только явно заданная тенантом, без default схемы.
            temperature=primary.temperature if "temperature" in primary.model_fields_set else None,
        )
        loop = AgentLoop(self._llm_for(primary.provider), self._tools)
        async for event in loop.run_turn(ctx):
            yield event


def to_llm_messages(history: Iterable[ChatMessage]) -> Iterable[LLMMessage]:
    """Сообщения диалога для модели. Ответы ассистента без текста (сбой до первого куска)
    пропускаются: провайдеры не принимают пустые сообщения."""
    for message in history:
        if message.role is MessageRole.USER:
            yield UserMessage(_user_text(message))
        elif message.content:
            yield AssistantMessage(message.content)


def _user_text(message: ChatMessage) -> str:
    user_input: dict[str, Any] = message.input or {}
    match user_input.get("type"):
        case "action":
            payload = user_input.get("payload")
            details = f" {_json(payload)}" if payload else ""
            return f"[Нажата кнопка {user_input['action_id']}{details}]"
        case "form_submit":
            return f"[Отправлена форма {user_input['form_id']}: {_json(user_input['values'])}]"
        case _:
            return message.content


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)
