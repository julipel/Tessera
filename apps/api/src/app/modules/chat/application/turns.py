"""Ход ассистента: события по SSE-протоколу (docs/contracts.md §2) и запись ответа."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import structlog

from app.contracts import Event, UserInput
from app.modules.chat.application.conversations import require_conversation, user_message
from app.modules.chat.domain.entities import (
    AgentTextDelta,
    Conversation,
    MessageRole,
    MessageStatus,
    NewMessage,
    TurnRequest,
)
from app.modules.chat.domain.errors import DuplicateMessageError
from app.modules.chat.domain.ports import ConversationStore, MessageStore, TurnAgent
from app.modules.shared.kernel import TenantId

PROTOCOL_VERSION = "1"
TEXT_BLOCK_ID = "b1"

logger = structlog.get_logger(__name__)

type Commit = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _AgentFinished:
    """Последний элемент очереди событий агента: чем закончилась работа агента."""

    status: MessageStatus


class TurnStream:
    """Один ход: `events()` отдаёт события хода, `save()` записывает ответ ассистента.

    message_id назначается заранее, чтобы события до записи сообщения уже несли id сообщения.
    Агент работает в отдельной задаче и передаёт события через очередь: так `cancel()` может
    прервать агента, пока стрим ждёт, и стрим всё равно закончится событием `done`.
    """

    def __init__(
        self,
        conversation: Conversation,
        user_input: dict[str, Any],
        agent: TurnAgent,
        messages: MessageStore,
        commit: Commit,
        registry: "TurnRegistry",
    ) -> None:
        self.conversation = conversation
        self.user_input = user_input
        self.agent = agent
        self.messages = messages
        self.commit = commit
        self.registry = registry
        self.turn_id = uuid4()
        self.message_id = uuid4()
        self._seq = 0
        self._text: list[str] = []
        self._saved = False
        self._cancel_requested = False
        self._agent_task: asyncio.Task[None] | None = None

    async def events(self) -> AsyncGenerator[Event]:
        # В реестре только начатые стримы: снимает с реестра close() при закрытии стрима.
        self.registry.add(self)
        # message_id нет только у turn_started (envelope.schema.json).
        yield self._event("turn_started", {})
        status = MessageStatus.INTERRUPTED
        if not self._cancel_requested:
            queue: asyncio.Queue[AgentTextDelta | _AgentFinished] = asyncio.Queue()
            self._agent_task = asyncio.create_task(self._run_agent(queue))
            self._agent_task.add_done_callback(partial(_report_cancelled, queue))
            while not isinstance(item := await queue.get(), _AgentFinished):
                self._text.append(item.text)
                yield self._event("text_delta", {"block_id": TEXT_BLOCK_ID, "delta": item.text})
            status = item.status
        if status is MessageStatus.FAILED:
            await self.save(status)
            yield self._event(
                "error", {"code": "internal", "message": "не удалось ответить", "retryable": False}
            )
            yield self._event("done", {"status": status})
            return
        if self._text:
            yield self._event("text_done", {"block_id": TEXT_BLOCK_ID})
        await self.save(status)
        yield self._event("done", {"status": status})

    async def _run_agent(self, queue: asyncio.Queue[AgentTextDelta | _AgentFinished]) -> None:
        request = TurnRequest(
            tenant_id=self.conversation.tenant_id,
            conversation_id=self.conversation.id,
            agent_config_id=self.conversation.agent_config_id,
            turn_id=self.turn_id,
            input=self.user_input,
        )
        try:
            async for chunk in self.agent.run_turn(request):
                if chunk.text:
                    queue.put_nowait(chunk)
        except Exception:
            logger.exception("turn_agent_failed", turn_id=str(self.turn_id))
            queue.put_nowait(_AgentFinished(MessageStatus.FAILED))
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
        """Записать ответ с накопленным текстом. Только первый вызов: после `done`
        повторный вызов (например, при закрытии стрима) ничего не меняет."""
        if self._saved:
            return
        self._saved = True
        text = "".join(self._text)
        blocks = ({"type": "text", "block_id": TEXT_BLOCK_ID, "text": text},) if text else ()
        await self.messages.add_once(
            self.conversation.tenant_id,
            NewMessage(
                id=self.message_id,
                conversation_id=self.conversation.id,
                role=MessageRole.ASSISTANT,
                status=status,
                content=text,
                blocks=blocks,
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
    agent: TurnAgent,
    commit: Commit,
    registry: "TurnRegistry",
) -> TurnStream:
    """Записать ввод пользователя (и зафиксировать до начала стрима) и подготовить ход.

    Повтор client_message_id — DuplicateMessageError: ход по этому вводу уже запускался,
    а ответ на него клиент берёт из истории."""
    conversation = await require_conversation(tenant_id, conversation_id, conversations)
    message, created = await messages.add_once(
        tenant_id, user_message(conversation_id, client_message_id, user_input)
    )
    if not created:
        raise DuplicateMessageError(f"сообщение {client_message_id} уже отправлено")
    await commit()
    return TurnStream(conversation, message.input or {}, agent, messages, commit, registry)


def _report_cancelled(
    queue: asyncio.Queue[AgentTextDelta | _AgentFinished], task: asyncio.Task[None]
) -> None:
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
