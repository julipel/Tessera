"""Ход ассистента: события агента → SSE-протокол (docs/contracts.md §2) и запись ответа."""

import asyncio
from collections.abc import AsyncGenerator, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import structlog

from app.contracts import AgentConfig, Event, FormSubmitInput, UserInput
from app.modules.agent.kernel import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    DialogStateUpdated,
    LLMError,
    SuggestionsOffered,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from app.modules.chat.application.conversations import require_conversation, user_message
from app.modules.chat.application.summaries import (
    SummaryScheduler,
    after_summary,
    needs_summary,
)
from app.modules.chat.domain.entities import (
    ChatMessage,
    Conversation,
    MessageRole,
    MessageStatus,
    NewMessage,
    ToolCallEntry,
    TurnRequest,
)
from app.modules.chat.domain.errors import (
    AgentConfigMissingError,
    DuplicateMessageError,
    InvalidInputError,
    MessageNotFoundError,
    RetryNotAllowedError,
)
from app.modules.chat.domain.ports import (
    AgentConfigSource,
    Commit,
    ConversationStore,
    MessageStore,
    ToolCallStore,
    TurnAgent,
)
from app.modules.shared.kernel import TenantId
from app.modules.tools.public import form_values_problem

PROTOCOL_VERSION = "1"

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _TurnError:
    """Данные SSE-события `error` (код — из docs/contracts.md §6)."""

    code: str
    message: str
    retryable: bool


_INTERNAL = _TurnError("internal", "не удалось ответить", retryable=False)


@dataclass(frozen=True, slots=True)
class _AgentFinished:
    """Последний элемент очереди событий агента: чем закончилась работа агента."""

    status: MessageStatus
    error: _TurnError | None = None


type _QueueItem = AgentEvent | _AgentFinished


class TurnStream:
    """Один ход: `events()` отдаёт события хода, `save()` записывает ответ ассистента.

    message_id назначается заранее, чтобы события до записи сообщения уже несли id сообщения
    (при повторе — тот, которым помечен заменённый ответ).
    Агент работает в отдельной задаче и передаёт события через очередь: так `cancel()` может
    прервать агента, пока стрим ждёт, и стрим всё равно закончится событием `done`.

    Блоки ответа — в порядке первого появления: текст до инструмента или компонента — один
    блок, текст после — новый. `tool_started` и `component` закрывают открытый текстовый блок.
    Завершённые вызовы инструментов и последнее состояние диалога записываются вместе
    с ответом; аргументы, результаты инструментов и состояние в клиент не уходят.
    После записи ответа длинная история сворачивается в сводку в фоне (`summaries`).
    """

    def __init__(
        self,
        conversation: Conversation,
        request: TurnRequest,
        agent: TurnAgent,
        conversations: ConversationStore,
        messages: MessageStore,
        tool_calls: ToolCallStore,
        commit: Commit,
        registry: "TurnRegistry",
        message_id: UUID | None = None,
        summaries: SummaryScheduler | None = None,
    ) -> None:
        self.conversation = conversation
        self.conversations = conversations
        self.request = request
        self.agent = agent
        self.messages = messages
        self.tool_calls = tool_calls
        self.commit = commit
        self.registry = registry
        self.summaries = summaries
        self.turn_id = request.turn_id
        self.message_id = message_id or uuid4()
        self._seq = 0
        self._blocks: list[dict[str, Any]] = []
        self._open_text: dict[str, Any] | None = None
        self._tools_started = 0
        self._finished_calls: list[ToolCallEntry] = []
        self._dialog_state: dict[str, Any] | None = None  # None — состояние не менялось
        self._usage: dict[str, int] | None = None
        self._saved = False
        self._cancel_requested = False
        self._agent_task: asyncio.Task[None] | None = None

    async def events(self) -> AsyncGenerator[Event]:
        # В реестре только начатые стримы: снимает с реестра close() при закрытии стрима.
        self.registry.add(self)
        # message_id нет только у turn_started (envelope.schema.json).
        yield self._event("turn_started", {})
        finished = _AgentFinished(MessageStatus.INTERRUPTED)
        if not self._cancel_requested:
            queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
            self._agent_task = asyncio.create_task(self._run_agent(queue))
            self._agent_task.add_done_callback(partial(_report_cancelled, queue))
            while not isinstance(item := await queue.get(), _AgentFinished):
                for event in self._translate(item):
                    yield event
            finished = item
        if finished.error is not None:
            await self.save(finished.status, finished.error)
            yield self._event(
                "error",
                {
                    "code": finished.error.code,
                    "message": finished.error.message,
                    "retryable": finished.error.retryable,
                },
            )
            yield self._event("done", {"status": finished.status})
            return
        for event in self._close_text():
            yield event
        await self.save(finished.status)
        yield self._event("done", {"status": finished.status, "usage": self._usage})

    def _translate(self, event: AgentEvent) -> Iterator[Event]:
        match event:
            case AnswerDelta(text=text) if text:
                if self._open_text is None:
                    self._open_text = self._add_block({"type": "text", "text": ""})
                self._open_text["text"] += text
                yield self._event(
                    "text_delta", {"block_id": self._open_text["block_id"], "delta": text}
                )
            case ToolStarted(tool_call_id=call_id, name=name, display_label=label):
                yield from self._close_text()
                self._tools_started += 1
                yield self._event(
                    "tool_started", {"tool_call_id": call_id, "name": name, "display_label": label}
                )
            case ToolFinished() as finished:
                self._finished_calls.append(_tool_call_entry(finished))
                yield self._event(
                    "tool_finished",
                    {
                        "tool_call_id": finished.tool_call_id,
                        "ok": finished.ok,
                        "duration_ms": finished.duration_ms,
                    },
                )
            case ComponentEmitted(component=component):
                yield from self._close_text()
                block = self._add_block({"type": "component", "component": component})
                yield self._event(
                    "component", {"block_id": block["block_id"], "component": component}
                )
            case SuggestionsOffered(items=items):
                # Нажатие быстрого ответа — обычный текст пользователя (contracts.md §2).
                yield self._event(
                    "suggestions",
                    {"items": [{"label": i, "input": {"type": "text", "text": i}} for i in items]},
                )
            case DialogStateUpdated(state=state):
                self._dialog_state = state.to_dict()
            case TurnCompleted(usage=usage):
                self._usage = {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "tool_calls": self._tools_started,
                }

    def _add_block(self, block: dict[str, Any]) -> dict[str, Any]:
        block = {"type": block["type"], "block_id": f"b{len(self._blocks) + 1}", **block}
        self._blocks.append(block)
        return block

    def _close_text(self) -> Iterator[Event]:
        if self._open_text is not None:
            yield self._event("text_done", {"block_id": self._open_text["block_id"]})
            self._open_text = None

    async def _run_agent(self, queue: asyncio.Queue[_QueueItem]) -> None:
        try:
            async for event in self.agent.run_turn(self.request):
                queue.put_nowait(event)
        except LLMError as e:
            logger.warning(
                "turn_llm_failed", turn_id=str(self.turn_id), error=str(e), retryable=e.retryable
            )
            error = _TurnError("llm_unavailable", "модель сейчас недоступна", e.retryable)
            queue.put_nowait(_AgentFinished(MessageStatus.FAILED, error))
            return
        except Exception:
            logger.exception("turn_agent_failed", turn_id=str(self.turn_id))
            queue.put_nowait(_AgentFinished(MessageStatus.FAILED, _INTERNAL))
            return
        queue.put_nowait(_AgentFinished(MessageStatus.COMPLETED))

    def cancel(self) -> None:
        """Прервать агента; стрим допишет накопленное и закончится `done{interrupted}`."""
        self._cancel_requested = True
        if self._agent_task is not None:
            self._agent_task.cancel()

    async def close(self) -> None:
        """При закрытии стрима (в т.ч. обрыве соединения): остановить агента, записать
        ответ как `interrupted`, если он ещё не записан, и снять ход с реестра."""
        try:
            if self._agent_task is not None and not self._agent_task.done():
                self._agent_task.cancel()
                await asyncio.wait([self._agent_task])
            await self.save(MessageStatus.INTERRUPTED)
        finally:
            self.registry.remove(self)

    async def save(self, status: MessageStatus, error: _TurnError | None = None) -> None:
        """Записать ответ с накопленными блоками, ошибкой хода и завершёнными вызовами
        инструментов (вызовы, прерванные отменой, не записываются). Только первый вызов:
        после `done` повторный вызов (например, при закрытии стрима) ничего не меняет."""
        if self._saved:
            return
        self._saved = True
        texts = [b["text"] for b in self._blocks if b["type"] == "text"]
        content = "\n\n".join(texts)
        await self.messages.add_once(
            self.conversation.tenant_id,
            NewMessage(
                id=self.message_id,
                conversation_id=self.conversation.id,
                role=MessageRole.ASSISTANT,
                status=status,
                content=content,
                blocks=tuple(self._blocks),
                error=(
                    {"code": error.code, "message": error.message, "retryable": error.retryable}
                    if error
                    else None
                ),
            ),
        )
        if self._finished_calls:
            await self.tool_calls.add_many(
                self.conversation.tenant_id, self.message_id, self._finished_calls
            )
        if self._dialog_state is not None:
            await self.conversations.update_state(
                self.conversation.tenant_id, self.conversation.id, self._dialog_state
            )
        await self.commit()
        logger.info("turn_finished", turn_id=str(self.turn_id), status=status)
        if self.summaries is not None and needs_summary(
            self.request.agent_config, self.request.history, content
        ):
            self.summaries.schedule(self.conversation.tenant_id, self.conversation.id)

    def _event(self, type_: str, data: dict[str, Any]) -> Event:
        self._seq += 1
        return Event.model_validate(
            {
                "protocol_version": PROTOCOL_VERSION,
                "seq": self._seq,
                "type": type_,
                "conversation_id": str(self.conversation.id),
                "turn_id": str(self.turn_id),
                "message_id": None if type_ == "turn_started" else str(self.message_id),
                "ts": datetime.now(UTC),
                "data": data,
            }
        )


async def start_turn(
    tenant_id: TenantId,
    conversation_id: UUID,
    client_message_id: UUID,
    user_input: UserInput,
    conversations: ConversationStore,
    messages: MessageStore,
    tool_calls: ToolCallStore,
    configs: AgentConfigSource,
    agent: TurnAgent,
    commit: Commit,
    registry: "TurnRegistry",
    summaries: SummaryScheduler | None = None,
) -> TurnStream:
    """Загрузить версию AgentConfig диалога, проверить и записать ввод пользователя
    (и зафиксировать до начала стрима) и подготовить ход: загрузить историю.

    Отправка формы, которой нет в конфиге, или с неподходящими значениями —
    InvalidInputError. Повтор client_message_id — DuplicateMessageError: ход по этому вводу
    уже запускался, а ответ на него клиент берёт из истории."""
    conversation, config = await _conversation_with_config(
        tenant_id, conversation_id, conversations, configs
    )
    if isinstance(user_input.root, FormSubmitInput):
        _check_form_submit(config, user_input.root)
    message, created = await messages.add_once(
        tenant_id, user_message(conversation_id, client_message_id, user_input)
    )
    if not created:
        raise DuplicateMessageError(f"сообщение {client_message_id} уже отправлено")
    await commit()
    request = _turn_request(
        conversation, config, message, await messages.list_for(tenant_id, conversation_id)
    )
    return TurnStream(
        conversation,
        request,
        agent,
        conversations,
        messages,
        tool_calls,
        commit,
        registry,
        summaries=summaries,
    )


async def retry_turn(
    tenant_id: TenantId,
    conversation_id: UUID,
    message_id: UUID,
    conversations: ConversationStore,
    messages: MessageStore,
    tool_calls: ToolCallStore,
    configs: AgentConfigSource,
    agent: TurnAgent,
    commit: Commit,
    registry: "TurnRegistry",
    summaries: SummaryScheduler | None = None,
) -> TurnStream:
    """Новый ход для сохранённого ввода вместо неудачного ответа `message_id` (ADR-0023).

    Ответ помечается заменённым новым ответом (и пометка фиксируется до начала стрима):
    в истории клиента и контексте модели остаётся один ввод и новый ответ. Повторить можно
    только последний ответ диалога с повторяемой ошибкой — иначе RetryNotAllowedError;
    ответа нет в диалоге (или он уже заменён) — MessageNotFoundError."""
    conversation, config = await _conversation_with_config(
        tenant_id, conversation_id, conversations, configs
    )
    history = list(await messages.list_for(tenant_id, conversation_id))
    failed = next((m for m in history if m.id == message_id), None)
    if failed is None:
        raise MessageNotFoundError(f"сообщения {message_id} в диалоге нет")
    if not _retryable(failed) or failed is not history[-1]:
        raise RetryNotAllowedError(f"ответ {message_id} нельзя повторить")
    history.pop()
    question = next((m for m in reversed(history) if m.role is MessageRole.USER), None)
    if question is None:
        raise RetryNotAllowedError(f"у ответа {message_id} нет ввода пользователя")
    new_message_id = uuid4()
    if not await messages.mark_replaced(tenant_id, message_id, new_message_id):
        raise RetryNotAllowedError(f"ответ {message_id} уже повторён")
    await commit()
    logger.info("turn_retry", replaced_message_id=str(message_id))
    request = _turn_request(conversation, config, question, history)
    return TurnStream(
        conversation,
        request,
        agent,
        conversations,
        messages,
        tool_calls,
        commit,
        registry,
        message_id=new_message_id,
        summaries=summaries,
    )


def _retryable(message: ChatMessage) -> bool:
    return (
        message.role is MessageRole.ASSISTANT
        and message.status is MessageStatus.FAILED
        and bool((message.error or {}).get("retryable"))
    )


async def _conversation_with_config(
    tenant_id: TenantId,
    conversation_id: UUID,
    conversations: ConversationStore,
    configs: AgentConfigSource,
) -> tuple[Conversation, dict[str, Any]]:
    conversation = await require_conversation(tenant_id, conversation_id, conversations)
    config = await configs.config(tenant_id, conversation.agent_config_id)
    if config is None:
        raise AgentConfigMissingError(f"нет AgentConfig {conversation.agent_config_id}")
    return conversation, config


def _turn_request(
    conversation: Conversation,
    config: dict[str, Any],
    question: ChatMessage,
    history: Sequence[ChatMessage],
) -> TurnRequest:
    # Свёрнутые в сводку сообщения модель видит только через сводку (architecture.md §7).
    summary, recent = after_summary(conversation, history)
    return TurnRequest(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        agent_config_id=conversation.agent_config_id,
        turn_id=uuid4(),
        input=question.input or {},
        agent_config=config,
        history=tuple(recent),
        dialog_state=conversation.state,
        history_summary=summary,
    )


def _check_form_submit(config: dict[str, Any], submitted: FormSubmitInput) -> None:
    form = (AgentConfig.model_validate(config).forms or {}).get(submitted.form_id)
    if form is None:
        raise InvalidInputError(f"формы {submitted.form_id!r} нет")
    if problem := form_values_problem(form, submitted.values):
        raise InvalidInputError(problem)


def _tool_call_entry(finished: ToolFinished) -> ToolCallEntry:
    error = (
        {"code": finished.error_code, "message": finished.error_message or ""}
        if finished.error_code
        else None
    )
    return ToolCallEntry(
        tool_call_id=finished.tool_call_id,
        name=finished.name,
        arguments=finished.arguments if finished.arguments is not None else {},
        result=None if error else finished.content,
        error=error,
        duration_ms=finished.duration_ms,
    )


def _report_cancelled(queue: asyncio.Queue[_QueueItem], task: asyncio.Task[None]) -> None:
    # Отменённая задача (в т.ч. до первого шага, когда её код не успел выполниться)
    # сама в очередь ничего не кладёт — сообщаем стриму за неё.
    if task.cancelled():
        queue.put_nowait(_AgentFinished(MessageStatus.INTERRUPTED))


class TurnRegistry:
    """Текущие ходы процесса — для явной отмены (POST .../turns/{turn_id}/cancel).

    Только в памяти процесса: при нескольких воркерах отмена срабатывает, лишь если
    попала в процесс со стримом. Межпроцессная отмена — P7.
    """

    def __init__(self) -> None:
        self._turns: dict[UUID, TurnStream] = {}

    def __contains__(self, turn_id: object) -> bool:
        return turn_id in self._turns

    def add(self, turn: TurnStream) -> None:
        self._turns[turn.turn_id] = turn

    def remove(self, turn: TurnStream) -> None:
        self._turns.pop(turn.turn_id, None)

    def cancel(self, tenant_id: TenantId, conversation_id: UUID, turn_id: UUID) -> bool:
        """False — хода нет, он завершён или чужой (тенант/диалог): для клиента это одно и то же."""
        turn = self._turns.get(turn_id)
        if turn is None or (turn.conversation.tenant_id, turn.conversation.id) != (
            tenant_id,
            conversation_id,
        ):
            return False
        turn.cancel()
        logger.info("turn_cancel_requested", turn_id=str(turn_id))
        return True
