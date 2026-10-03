"""Ход ассистента: события агента → SSE-протокол (docs/contracts.md §2) и запись ответа."""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import structlog

from app.contracts import Event, UserInput
from app.modules.agent.kernel import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    LLMError,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from app.modules.chat.application.conversations import require_conversation, user_message
from app.modules.chat.domain.entities import (
    Conversation,
    MessageRole,
    MessageStatus,
    NewMessage,
    TurnRequest,
)
from app.modules.chat.domain.errors import AgentConfigMissingError, DuplicateMessageError
from app.modules.chat.domain.ports import (
    AgentConfigSource,
    ConversationStore,
    MessageStore,
    TurnAgent,
)
from app.modules.shared.kernel import TenantId

PROTOCOL_VERSION = "1"

logger = structlog.get_logger(__name__)

type Commit = Callable[[], Awaitable[None]]


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

    message_id назначается заранее, чтобы события до записи сообщения уже несли id сообщения.
    Агент работает в отдельной задаче и передаёт события через очередь: так `cancel()` может
    прервать агента, пока стрим ждёт, и стрим всё равно закончится событием `done`.

    Блоки ответа — в порядке первого появления: текст до инструмента или компонента — один
    блок, текст после — новый. `tool_started` и `component` закрывают открытый текстовый блок.
    """

    def __init__(
        self,
        conversation: Conversation,
        request: TurnRequest,
        agent: TurnAgent,
        messages: MessageStore,
        commit: Commit,
        registry: "TurnRegistry",
    ) -> None:
        self.conversation = conversation
        self.request = request
        self.agent = agent
        self.messages = messages
        self.commit = commit
        self.registry = registry
        self.turn_id = request.turn_id
        self.message_id = uuid4()
        self._seq = 0
        self._blocks: list[dict[str, Any]] = []
        self._open_text: dict[str, Any] | None = None
        self._tools_started: dict[str, float] = {}
        self._tool_calls = 0
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
            await self.save(finished.status)
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
            case ToolStarted(tool_call_id=call_id, name=name):
                yield from self._close_text()
                self._tool_calls += 1
                self._tools_started[call_id] = time.perf_counter()
                yield self._event(
                    "tool_started", {"tool_call_id": call_id, "name": name, "display_label": None}
                )
            case ToolFinished(tool_call_id=call_id, ok=ok):
                # От раннего tool_started (ещё до аргументов) до результата: верхняя оценка
                # длительности инструмента, включает стрим аргументов моделью.
                started = self._tools_started.pop(call_id, time.perf_counter())
                yield self._event(
                    "tool_finished",
                    {
                        "tool_call_id": call_id,
                        "ok": ok,
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                    },
                )
            case ComponentEmitted(component=component):
                yield from self._close_text()
                block = self._add_block({"type": "component", "component": component})
                yield self._event(
                    "component", {"block_id": block["block_id"], "component": component}
                )
            case TurnCompleted(usage=usage):
                self._usage = {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "tool_calls": self._tool_calls,
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

    async def save(self, status: MessageStatus) -> None:
        """Записать ответ с накопленными блоками. Только первый вызов: после `done`
        повторный вызов (например, при закрытии стрима) ничего не меняет."""
        if self._saved:
            return
        self._saved = True
        texts = [b["text"] for b in self._blocks if b["type"] == "text"]
        await self.messages.add_once(
            self.conversation.tenant_id,
            NewMessage(
                id=self.message_id,
                conversation_id=self.conversation.id,
                role=MessageRole.ASSISTANT,
                status=status,
                content="\n\n".join(texts),
                blocks=tuple(self._blocks),
            ),
        )
        await self.commit()
        logger.info("turn_finished", turn_id=str(self.turn_id), status=status)

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
    configs: AgentConfigSource,
    agent: TurnAgent,
    commit: Commit,
    registry: "TurnRegistry",
) -> TurnStream:
    """Записать ввод пользователя (и зафиксировать до начала стрима) и подготовить ход:
    загрузить версию AgentConfig диалога и историю.

    Повтор client_message_id — DuplicateMessageError: ход по этому вводу уже запускался,
    а ответ на него клиент берёт из истории."""
    conversation = await require_conversation(tenant_id, conversation_id, conversations)
    message, created = await messages.add_once(
        tenant_id, user_message(conversation_id, client_message_id, user_input)
    )
    if not created:
        raise DuplicateMessageError(f"сообщение {client_message_id} уже отправлено")
    await commit()
    config = await configs.config(tenant_id, conversation.agent_config_id)
    if config is None:
        raise AgentConfigMissingError(f"нет AgentConfig {conversation.agent_config_id}")
    request = TurnRequest(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        agent_config_id=conversation.agent_config_id,
        turn_id=uuid4(),
        input=message.input or {},
        agent_config=config,
        history=tuple(await messages.list_for(tenant_id, conversation_id)),
    )
    return TurnStream(conversation, request, agent, messages, commit, registry)


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
