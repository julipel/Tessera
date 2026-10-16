"""TurnAgent поверх агентного цикла (модуль agent): AgentConfig, история и DialogState →
TurnContext.

В БД не ходит: конфиг и история приходят в TurnRequest (их загружает start_turn).
"""

import json
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog

from app.contracts import AgentConfig
from app.modules.agent.public import (
    AgentEvent,
    AgentLoop,
    AssistantMessage,
    ConfirmationReply,
    DialogStateUpdated,
    FallbackLLM,
    FallbackModel,
    LLMClient,
    LLMMessage,
    Provider,
    RegistryToolExecutor,
    RuntimeContext,
    ToolExecutor,
    TracedLLM,
    TurnContext,
    TurnLimits,
    UserMessage,
    build_system_prompt,
    traced_turn,
)
from app.modules.chat.domain.entities import ChatMessage, MessageRole, TurnRequest
from app.modules.memory.public import DialogState
from app.modules.observability.kernel import NoopTracer, Tracer, TurnTrace, TurnTraceInfo
from app.modules.tenants.kernel import (
    Language,
    assistant_texts,
    localize_config,
    resolve_language,
)
from app.modules.tools.public import (
    Catalog,
    ConfirmLabels,
    KnowledgeSearcher,
    LeadStore,
    ToolRegistry,
    builtin_tools,
    confirm_labels,
    pressed_confirmation,
)

type LLMForProvider = Callable[[Provider], LLMClient]
type ToolsForConfig = Callable[[AgentConfig, Language], ToolExecutor]

logger = structlog.get_logger(__name__)


class LoopTurnAgent:
    """`llm_for` — клиент по провайдеру из AgentConfig (в приложении — `LLMClients.for_provider`),
    `tools_for` — инструменты хода по AgentConfig и языку диалога, `now` — часы для
    Runtime-слоя промпта и срока жизни подтверждения (в тестах фиксированные), `tracer` —
    трейсы ходов (Langfuse, ADR-0029; по умолчанию выключены)."""

    def __init__(
        self,
        llm_for: LLMForProvider,
        tools_for: ToolsForConfig,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        tracer: Tracer | None = None,
    ) -> None:
        self._llm_for = llm_for
        self._tools_for = tools_for
        self._now = now
        self._tracer = tracer or NoopTracer()

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        config = AgentConfig.model_validate(request.agent_config)
        primary = config.model.primary
        limits = config.limits
        state, problems = DialogState.parse(request.dialog_state)
        if problems:
            # Повреждённые части отброшены; исправленное состояние уходит событием в начале
            # хода и записывается в конце, даже если инструменты состояние не меняли.
            logger.warning(
                "dialog_state_invalid",
                tenant_id=str(request.tenant_id),
                conversation_id=str(request.conversation_id),
                turn_id=str(request.turn_id),
                problems=problems,
            )
        now = self._now()
        pending = state.pending_confirmation
        expired = False
        if pending is not None and pending.expired(now):
            expired = True
            # Проигнорированная кнопка не исполнит устаревшие аргументы (дополнение к ADR-0021):
            # нажатие получит «подтверждение устарело», состояние без ожидания запишется.
            logger.info(
                "confirmation_expired",
                tenant_id=str(request.tenant_id),
                conversation_id=str(request.conversation_id),
                turn_id=str(request.turn_id),
                confirm_id=pending.confirm_id,
                tool=pending.tool,
            )
            state = state.apply({"pending_confirmation": None})
        # Диалог без языка (создан до P6-04) — язык по конфигу, как без locale (ADR-0025).
        language = resolve_language(config.assistant, request.language)
        # Слоты неактивных сценариев модели не нужны (ADR-0026); ожидающий вызов — не знание
        # о пользователе: о нём модель узнаёт из результата инструмента и пометки к ответу.
        known = state.prompt_dict()
        runtime = RuntimeContext(
            now=now,
            dialog_state=known,
            active_scenario=state.active_scenario,
            history_summary=request.history_summary,
            # Язык интерфейса — запасной язык ответа, только пока клиент ничего не написал
            # текстом: иначе модель отвечает на нём, хотя клиент пишет на другом (P6-04c).
            language=None if _has_text(request.history) else language,
            client_script=client_script(request.history),
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
                turn_timeout_s=limits.turn_timeout_s or 60,
                max_tool_calls_per_step=limits.max_tool_calls_per_step or 4,
                max_output_tokens=limits.max_output_tokens or 4096,
                max_turn_tokens=limits.max_turn_tokens or 120_000,
            ),
            fallback_message=assistant_texts(config.assistant, language).fallback_message,
            temperature=primary.temperature,  # подсказка: применяет адаптер (ADR-0009)
            state=state,
            confirmation=confirmation_reply(request.input),
        )
        trace = self._tracer.start_turn(
            TurnTraceInfo(
                trace_id=request.trace_id,
                tenant_id=request.tenant_id,
                conversation_id=request.conversation_id,
                turn_id=request.turn_id,
                input=request.input,
                prompt_versions={
                    "platform_prompt": prompt.platform_version,
                    "agent_config": str(request.agent_config_id),
                },
            )
        )
        loop = AgentLoop(self._llm(config, trace), self._tools_for(config, language))
        if problems or expired:
            yield DialogStateUpdated(state)
        async for event in traced_turn(loop.run_turn(ctx), trace):
            yield event

    def _llm(self, config: AgentConfig, trace: TurnTrace) -> LLMClient:
        """Основная модель с ретраями и резервной из `model.fallback` (ADR-0027); клиент
        резервной создаётся, только когда понадобится. Каждая попытка — generation в трейсе."""
        primary_provider = config.model.primary.provider
        primary = TracedLLM(self._llm_for(primary_provider), trace, primary_provider)
        fallback = config.model.fallback
        if fallback is None:
            return FallbackLLM(primary)
        provider = fallback.provider
        return FallbackLLM(
            primary,
            FallbackModel(
                client=lambda: TracedLLM(self._llm_for(provider), trace, provider),
                model=fallback.name,
                temperature=fallback.temperature,
            ),
        )


def builtin_turn_agent(
    llm_for: LLMForProvider,
    knowledge: KnowledgeSearcher | None = None,
    catalog: Catalog | None = None,
    leads: LeadStore | None = None,
    tracer: Tracer | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LoopTurnAgent:
    """Агентный цикл со встроенными инструментами из `tools.builtin` конфига — так ход
    собирают chat API и раннер эвалов (ADR-0011). Без `knowledge` не подключается
    `search_knowledge`, без `catalog` — `search_catalog` и `get_entity`, без `leads` —
    `create_lead`. Формы и подписи каталога — на языке диалога (ADR-0025). Без `tracer`
    ходы не трейсятся. `now` — одни часы для промпта, времени создания подтверждения
    и его срока жизни."""
    return LoopTurnAgent(
        llm_for,
        lambda config, language: RegistryToolExecutor(
            ToolRegistry(
                builtin_tools(
                    localize_config(config, language),
                    knowledge=knowledge,
                    catalog=catalog,
                    leads=leads,
                    language=language,
                ),
                _confirm_labels(config, language),
                now,
            )
        ),
        now=now,
        tracer=tracer,
    )


def confirmation_reply(user_input: dict[str, Any]) -> ConfirmationReply | None:
    """Нажатие кнопки компонента confirm (ADR-0021); иначе None."""
    if (pressed := pressed_confirmation(user_input)) is None:
        return None
    confirm_id, approved = pressed
    return ConfirmationReply(confirm_id=confirm_id, approved=approved)


def _confirm_labels(config: AgentConfig, language: Language) -> ConfirmLabels:
    texts = assistant_texts(config.assistant, language)
    return confirm_labels(language, texts.confirm, texts.cancel)


def _has_text(history: Iterable[ChatMessage]) -> bool:
    return any(m.role is MessageRole.USER and m.content.strip() for m in history)


def client_script(history: Sequence[ChatMessage]) -> str | None:
    """Алфавит последнего текстового сообщения клиента: `cyrillic`, `latin` или None (нет
    текста или букв поровну). Язык ответа по-прежнему выбирает модель (ADR-0025); это сигнал
    против языка данных и инструкций — к нему модель и сбивалась (P6-04c)."""
    text = next(
        (m.content for m in reversed(history) if m.role is MessageRole.USER and m.content.strip()),
        "",
    )
    letters = [c.lower() for c in text if c.isalpha()]
    cyrillic = sum("а" <= c <= "я" or c == "ё" for c in letters)
    latin = sum("a" <= c <= "z" or c in "åäöéü" for c in letters)
    if cyrillic == latin:
        return None
    return "cyrillic" if cyrillic > latin else "latin"


def to_llm_messages(history: Iterable[ChatMessage]) -> Iterable[LLMMessage]:
    """Сообщения диалога для модели. Ответы ассистента без текста (сбой до первого куска)
    или из одних пробелов пропускаются: провайдеры не принимают пустые сообщения."""
    for message in history:
        if message.role is MessageRole.USER:
            yield UserMessage(_user_text(message))
        elif message.content.strip():
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
