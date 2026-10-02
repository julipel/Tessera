"""Ход ассистента: события по SSE-протоколу (docs/contracts.md §2) и запись ответа."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog

from app.contracts import Event, UserInput
from app.modules.chat.application.conversations import require_conversation, user_message
from app.modules.chat.domain.entities import (
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


class TurnStream:
    """Один ход: `events()` отдаёт события хода, `save()` записывает ответ ассистента.

    message_id назначается заранее, чтобы события до записи сообщения уже несли id сообщения.
    """

    def __init__(
        self,
        conversation: Conversation,
        user_input: dict[str, Any],
        agent: TurnAgent,
        messages: MessageStore,
        commit: Commit,
    ) -> None:
        self.conversation = conversation
        self.user_input = user_input
        self.agent = agent
        self.messages = messages
        self.commit = commit
        self.turn_id = uuid4()
        self.message_id = uuid4()
        self._seq = 0
        self._text: list[str] = []
        self._saved = False

    async def events(self) -> AsyncGenerator[Event]:
        # message_id нет только у turn_started (envelope.schema.json).
        yield self._event("turn_started", {})
        request = TurnRequest(
            tenant_id=self.conversation.tenant_id,
            conversation_id=self.conversation.id,
            agent_config_id=self.conversation.agent_config_id,
            input=self.user_input,
        )
        try:
            async for chunk in self.agent.run_turn(request):
                if not chunk.text:
                    continue
                self._text.append(chunk.text)
                yield self._event("text_delta", {"block_id": TEXT_BLOCK_ID, "delta": chunk.text})
        except Exception:
            logger.exception("turn_agent_failed", turn_id=str(self.turn_id))
            await self.save(MessageStatus.FAILED)
            yield self._event(
                "error", {"code": "internal", "message": "не удалось ответить", "retryable": False}
            )
            yield self._event("done", {"status": MessageStatus.FAILED})
            return
        if self._text:
            yield self._event("text_done", {"block_id": TEXT_BLOCK_ID})
        await self.save(MessageStatus.COMPLETED)
        yield self._event("done", {"status": MessageStatus.COMPLETED})

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
    return TurnStream(conversation, message.input or {}, agent, messages, commit)
