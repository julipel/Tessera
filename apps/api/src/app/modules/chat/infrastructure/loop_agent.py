"""TurnAgent поверх агентного цикла (модуль agent): AgentConfig, история и DialogState →
TurnContext.

В БД не ходит: конфиг и история приходят в TurnRequest (их загружает start_turn).
"""

import json
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from app.contracts import AgentConfig
from app.modules.agent.public import (
    AgentEvent,
    AgentLoop,
    AssistantMessage,
    ConfirmationReply,
    LLMClient,
    LLMMessage,
    Provider,
    RegistryToolExecutor,
    RuntimeContext,
    ToolExecutor,
    TurnContext,
    TurnLimits,
    UserMessage,
    build_system_prompt,
)
from app.modules.chat.domain.entities import ChatMessage, MessageRole, TurnRequest
from app.modules.memory.public import DialogState
from app.modules.tools.public import (
    CANCEL_ACTION_ID,
    CONFIRM_ACTION_ID,
    Catalog,
    ConfirmLabels,
    KnowledgeSearcher,
    LeadStore,
    ToolRegistry,
    builtin_tools,
)

type LLMForProvider = Callable[[Provider], LLMClient]
type ToolsForConfig = Callable[[AgentConfig], ToolExecutor]


class LoopTurnAgent:
    """`llm_for` — клиент по провайдеру из AgentConfig (в приложении — `LLMClients.for_provider`),
    `tools_for` — инструменты хода по AgentConfig, `now` — часы для Runtime-слоя промпта
    (в тестах фиксированные)."""

    def __init__(
        self,
        llm_for: LLMForProvider,
        tools_for: ToolsForConfig,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._llm_for = llm_for
        self._tools_for = tools_for
        self._now = now

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        config = AgentConfig.model_validate(request.agent_config)
        primary = config.model.primary
        limits = config.limits
        state = DialogState.from_dict(request.dialog_state)
        # Ожидающий вызов — не знание о пользователе: о нём модель узнаёт из результата
        # инструмента и пометки к ответу на подтверждение.
        known = {k: v for k, v in state.to_dict().items() if k != "pending_confirmation"}
        runtime = RuntimeContext(
            now=self._now(),
            dialog_state=known,
            active_scenario=state.active_scenario,
            history_summary=request.history_summary,
        )
        prompt = build_system_prompt(config, runtime)
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
            temperature=primary.temperature,  # подсказка: применяет адаптер (ADR-0009)
            state=state,
            confirmation=confirmation_reply(request.input),
        )
        loop = AgentLoop(self._llm_for(primary.provider), self._tools_for(config))
        async for event in loop.run_turn(ctx):
            yield event


def builtin_turn_agent(
    llm_for: LLMForProvider,
    knowledge: KnowledgeSearcher | None = None,
    catalog: Catalog | None = None,
    leads: LeadStore | None = None,
) -> LoopTurnAgent:
    """Агентный цикл со встроенными инструментами из `tools.builtin` конфига — так ход
    собирают chat API и раннер эвалов (ADR-0011). Без `knowledge` не подключается
    `search_knowledge`, без `catalog` — `search_catalog` и `get_entity`, без `leads` —
    `create_lead`."""
    return LoopTurnAgent(
        llm_for,
        lambda config: RegistryToolExecutor(
            ToolRegistry(
                builtin_tools(config, knowledge=knowledge, catalog=catalog, leads=leads),
                _confirm_labels(config),
            )
        ),
    )


def confirmation_reply(user_input: dict[str, Any]) -> ConfirmationReply | None:
    """Нажатие кнопки компонента confirm (ADR-0021); иначе None."""
    if user_input.get("type") != "action":
        return None
    action_id = user_input.get("action_id")
    confirm_id = (user_input.get("payload") or {}).get("confirm_id")
    if action_id not in (CONFIRM_ACTION_ID, CANCEL_ACTION_ID) or not isinstance(confirm_id, str):
        return None
    return ConfirmationReply(confirm_id=confirm_id, approved=action_id == CONFIRM_ACTION_ID)


def _confirm_labels(config: AgentConfig) -> ConfirmLabels:
    labels = config.assistant.confirm_labels
    if labels is None:
        return ConfirmLabels()
    default = ConfirmLabels()
    return ConfirmLabels(
        confirm=labels.confirm or default.confirm, cancel=labels.cancel or default.cancel
    )


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
            # Подпись — то, что видел пользователь; action_id и payload — для инструментов.
            label = user_input.get("label")
            button = f"«{label}» ({user_input['action_id']})" if label else user_input["action_id"]
            payload = user_input.get("payload")
            details = f" {_json(payload)}" if payload else ""
            return f"[Нажата кнопка {button}{details}]"
        case "form_submit":
            return f"[Отправлена форма {user_input['form_id']}: {_json(user_input['values'])}]"
        case _:
            return message.content


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)
