"""Сводка ранней истории (P6-02, architecture.md §7): политика сворачивания, вызов модели,
запись сводки, контекст хода после неё и запуск в фоне после хода (ADR-0024)."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import UserInput
from app.modules.agent.public import (
    SUMMARY_PROMPT_VERSION,
    AgentEvent,
    AnswerDelta,
    AssistantMessage,
    FakeLLM,
    FakeReply,
    HistorySummarizer,
    LLMError,
    Provider,
    RegistryToolExecutor,
    UserMessage,
)
from app.modules.chat.api.router import get_summary_scheduler, get_turn_agent
from app.modules.chat.application.summaries import SummaryScheduler, summarize_conversation
from app.modules.chat.application.turns import TurnRegistry, start_turn
from app.modules.chat.domain.entities import (
    Channel,
    ChatMessage,
    MessageRole,
    MessageStatus,
    NewMessage,
    TurnRequest,
)
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent
from app.modules.chat.infrastructure.repositories import (
    ConversationRepository,
    MessageRepository,
    TenantsAgentConfigs,
    ToolCallRepository,
)
from app.modules.chat.infrastructure.summarizer import LoopConversationSummarizer
from app.modules.memory.public import HistoryItem, estimate_tokens, fold_point
from app.modules.shared.public import TenantId
from app.modules.tenants.public import (
    AgentConfigRepository,
    SqlTenantDirectory,
    WidgetKeyRepository,
    hash_widget_key,
)
from app.modules.tools.public import ToolRegistry

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
LONG = "слово " * 300  # ~600 приблизительных токенов


def _config(memory: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "assistant": {"name": "A", "greeting": "Привет!", "fallback_message": "Не вышло."},
        "model": {"primary": {"provider": "openai", "name": "main-model"}},
        "limits": {},
        "prompt": {"tenant": "Ты — консультант."},
        "tools": {},
    }
    if memory is not None:
        config["memory"] = memory
    return config


SMALL_MEMORY = {"summary_threshold_tokens": 500, "keep_recent_turns": 1}


# --- Политика ---


def user(text: str) -> HistoryItem:
    return HistoryItem(from_user=True, text=text)


def bot(text: str) -> HistoryItem:
    return HistoryItem(from_user=False, text=text)


def test_estimate_tokens_by_length() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("абв") == 1
    assert estimate_tokens("a" * 30) == 10


def test_no_fold_below_threshold() -> None:
    items = [user("a" * 30), bot("b" * 30), user("c" * 30), bot("d" * 30)]

    assert fold_point(items, threshold_tokens=40, keep_recent_turns=1) is None


def test_no_fold_when_only_recent_turns() -> None:
    items = [user(LONG), bot(LONG), user(LONG), bot(LONG)]

    assert fold_point(items, threshold_tokens=500, keep_recent_turns=2) is None


def test_fold_keeps_recent_turns_from_turn_start() -> None:
    # Ход — ввод пользователя и все ответы на него (несколько ответов подряд — один ход).
    items = [user(LONG), bot(LONG), user(LONG), bot(LONG), bot(LONG), user(LONG), bot(LONG)]

    assert fold_point(items, threshold_tokens=500, keep_recent_turns=2) == 2
    assert fold_point(items, threshold_tokens=500, keep_recent_turns=1) == 5


# --- Вызов модели ---


async def test_summarizer_sends_previous_summary_and_messages() -> None:
    llm = FakeLLM([FakeReply(text="  - ищет крем для сухой кожи  ")])

    summary = await HistorySummarizer(llm).summarize(
        "m",
        "- бюджет 3000",
        (UserMessage("Нужен крем <для> сухой кожи"), AssistantMessage("Покажу варианты.")),
        temperature=0.2,
    )

    assert summary == "- ищет крем для сухой кожи"
    [request] = llm.requests
    assert (request.model, request.temperature, request.tools) == ("m", 0.2, ())
    assert request.max_output_tokens is not None
    assert "ничего не додумывай" in request.system
    [prompt] = request.messages
    assert isinstance(prompt, UserMessage)
    assert "<summary>\n- бюджет 3000\n</summary>" in prompt.text
    # Текст пользователя не закрывает тег данных.
    assert "Клиент: Нужен крем \\u003cдля> сухой кожи" in prompt.text
    assert "Консультант: Покажу варианты." in prompt.text
    assert SUMMARY_PROMPT_VERSION == "1"


async def test_summarizer_returns_none_for_empty_text() -> None:
    llm = FakeLLM([FakeReply(text="   ")])

    assert await HistorySummarizer(llm).summarize("m", None, (UserMessage("Привет"),)) is None
    assert "<summary>" not in llm.requests[0].messages[0].text  # type: ignore[union-attr]


def _message(role: MessageRole, content: str) -> ChatMessage:
    user_input = {"type": "text", "text": content} if role is MessageRole.USER else None
    return ChatMessage(
        id=uuid4(),
        tenant_id=TenantId(uuid4()),
        conversation_id=uuid4(),
        role=role,
        status=MessageStatus.COMPLETED,
        content=content,
        input=user_input,
        blocks=[],
        client_message_id=None,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        (None, ("openai", "main-model")),
        (
            {"summary_model": {"provider": "anthropic", "name": "cheap-model"}},
            ("anthropic", "cheap-model"),
        ),
    ],
)
async def test_summary_model_from_config(
    memory: dict[str, Any] | None, expected: tuple[str, str]
) -> None:
    llm = FakeLLM([FakeReply(text="сводка")])
    providers: list[Provider] = []

    def llm_for(provider: Provider) -> FakeLLM:
        providers.append(provider)
        return llm

    summary = await LoopConversationSummarizer(llm_for).summarize(
        _config(memory), None, [_message(MessageRole.USER, "Привет")]
    )

    assert summary == "сводка"
    assert (providers[0], llm.requests[0].model) == expected


async def test_turn_prompt_contains_history_summary() -> None:
    llm = FakeLLM([FakeReply(text="Продолжим.")])
    agent = LoopTurnAgent(
        lambda _: llm, lambda *_: RegistryToolExecutor(ToolRegistry([])), now=lambda: NOW
    )
    request = TurnRequest(
        tenant_id=TenantId(uuid4()),
        conversation_id=uuid4(),
        agent_config_id=uuid4(),
        turn_id=uuid4(),
        input={"type": "text", "text": "Дальше"},
        agent_config=_config(),
        history=(_message(MessageRole.USER, "Дальше"),),
        history_summary="- ищет крем для сухой кожи",
    )

    async for _ in agent.run_turn(request):
        pass

    assert "Сводка ранней части диалога:\n- ищет крем для сухой кожи" in llm.requests[0].system


# --- Запись сводки и ход после неё (БД) ---


async def _conversation(
    session: AsyncSession, slug: str, config: dict[str, Any]
) -> tuple[TenantId, UUID]:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    configs = AgentConfigRepository(session)
    draft = await configs.create_draft(tenant.id, config)
    await configs.activate(tenant.id, draft.id)
    conversation = await ConversationRepository(session).create(
        tenant.id, draft.id, Channel.WEB, "v-1"
    )
    return tenant.id, conversation.id


async def _add(
    session: AsyncSession, tenant_id: TenantId, conversation_id: UUID, *texts: str
) -> list[UUID]:
    """Сообщения по очереди: пользователь, ассистент, пользователь…"""
    ids = []
    for i, text in enumerate(texts):
        role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
        message, _ = await MessageRepository(session).add_once(
            tenant_id,
            NewMessage(
                conversation_id=conversation_id,
                role=role,
                status=MessageStatus.COMPLETED,
                content=text,
                input={"type": "text", "text": text} if role is MessageRole.USER else None,
                client_message_id=uuid4() if role is MessageRole.USER else None,
            ),
        )
        ids.append(message.id)
    return ids


async def _summarize(
    session: AsyncSession, tenant_id: TenantId, conversation_id: UUID, llm: FakeLLM
) -> bool:
    return await summarize_conversation(
        tenant_id,
        conversation_id,
        ConversationRepository(session),
        MessageRepository(session),
        TenantsAgentConfigs(session),
        LoopConversationSummarizer(lambda _: llm),
        session.commit,
    )


async def test_long_history_is_folded_except_recent_turns(db_session: AsyncSession) -> None:
    tenant_id, conversation_id = await _conversation(db_session, "shop", _config(SMALL_MEMORY))
    ids = await _add(db_session, tenant_id, conversation_id, "Q1 " + LONG, "A1", "Q2", "A2")
    llm = FakeLLM([FakeReply(text="- клиент спросил Q1")])

    assert await _summarize(db_session, tenant_id, conversation_id, llm)

    conversation = await ConversationRepository(db_session).find(tenant_id, conversation_id)
    assert conversation is not None
    assert (conversation.summary, conversation.summary_message_id) == (
        "- клиент спросил Q1",
        ids[1],
    )
    prompt = llm.requests[0].messages[0].text  # type: ignore[union-attr]
    assert "Консультант: A1" in prompt
    assert "Q2" not in prompt


async def test_next_summary_extends_previous_and_short_history_is_kept(
    db_session: AsyncSession,
) -> None:
    tenant_id, conversation_id = await _conversation(db_session, "shop", _config(SMALL_MEMORY))
    ids = await _add(db_session, tenant_id, conversation_id, "Q1 " + LONG, "A1", "Q2", "A2")
    llm = FakeLLM([FakeReply(text="- сводка 1"), FakeReply(text="- сводка 2")])
    await _summarize(db_session, tenant_id, conversation_id, llm)

    # После сводки история короткая — модель не вызывается.
    assert not await _summarize(db_session, tenant_id, conversation_id, llm)
    assert len(llm.requests) == 1

    ids += await _add(db_session, tenant_id, conversation_id, "Q3 " + LONG, "A3")
    assert await _summarize(db_session, tenant_id, conversation_id, llm)

    second = llm.requests[1].messages[0].text  # type: ignore[union-attr]
    assert "<summary>\n- сводка 1\n</summary>" in second
    assert "Клиент: Q2" in second
    assert "Q1" not in second and "Q3" not in second
    conversation = await ConversationRepository(db_session).find(tenant_id, conversation_id)
    assert conversation is not None
    assert (conversation.summary, conversation.summary_message_id) == ("- сводка 2", ids[3])


async def test_empty_summary_and_llm_error_keep_history(db_session: AsyncSession) -> None:
    tenant_id, conversation_id = await _conversation(db_session, "shop", _config(SMALL_MEMORY))
    await _add(db_session, tenant_id, conversation_id, "Q1 " + LONG, "A1", "Q2")
    llm = FakeLLM([FakeReply(text=""), LLMError("недоступна", retryable=True)])

    assert not await _summarize(db_session, tenant_id, conversation_id, llm)
    with pytest.raises(LLMError):
        await _summarize(db_session, tenant_id, conversation_id, llm)

    conversation = await ConversationRepository(db_session).find(tenant_id, conversation_id)
    assert conversation is not None
    assert (conversation.summary, conversation.summary_message_id) == (None, None)


async def test_update_summary_is_conditional_and_tenant_scoped(db_session: AsyncSession) -> None:
    tenant_id, conversation_id = await _conversation(db_session, "shop", _config())
    other, _ = await _conversation(db_session, "other", _config())
    first, second = await _add(db_session, tenant_id, conversation_id, "Q1", "A1")
    repo = ConversationRepository(db_session)

    assert not await repo.update_summary(other, conversation_id, "чужая", first, None)
    assert await repo.update_summary(tenant_id, conversation_id, "s1", first, None)
    # Опоздавшая сводка от той же границы не затирает записанную.
    assert not await repo.update_summary(tenant_id, conversation_id, "s1-late", first, None)
    assert await repo.update_summary(tenant_id, conversation_id, "s2", second, first)

    conversation = await repo.find(tenant_id, conversation_id)
    assert conversation is not None
    assert (conversation.summary, conversation.summary_message_id) == ("s2", second)


class CapturingAgent:
    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.requests.append(request)
        yield AnswerDelta("ответ")


async def test_turn_after_summary_gets_summary_instead_of_folded_messages(
    db_session: AsyncSession,
) -> None:
    tenant_id, conversation_id = await _conversation(db_session, "shop", _config())
    ids = await _add(db_session, tenant_id, conversation_id, "Q1", "A1", "Q2", "A2")
    await ConversationRepository(db_session).update_summary(
        tenant_id, conversation_id, "- спросил Q1", ids[1], None
    )
    agent = CapturingAgent()

    turn = await start_turn(
        tenant_id,
        conversation_id,
        uuid4(),
        UserInput.model_validate({"type": "text", "text": "Q3"}),
        ConversationRepository(db_session),
        MessageRepository(db_session),
        ToolCallRepository(db_session),
        TenantsAgentConfigs(db_session),
        agent,
        db_session.commit,
        TurnRegistry(),
    )
    async for _ in turn.events():
        pass

    [request] = agent.requests
    assert request.history_summary == "- спросил Q1"
    assert [m.content for m in request.history] == ["Q2", "A2", "Q3"]


# --- Запуск в фоне после хода ---


async def test_scheduler_runs_one_summary_per_conversation() -> None:
    release = asyncio.Event()
    runs: list[UUID] = []

    async def run(tenant_id: TenantId, conversation_id: UUID) -> bool:
        runs.append(conversation_id)
        await release.wait()
        return True

    scheduler = SummaryScheduler(run)
    tenant, first, second = TenantId(uuid4()), uuid4(), uuid4()

    assert scheduler.schedule(tenant, first)
    assert not scheduler.schedule(tenant, first)  # сводка этого диалога уже идёт
    assert scheduler.schedule(tenant, second)
    await asyncio.sleep(0)
    release.set()
    await scheduler.drain()

    assert runs == [first, second]
    assert scheduler.schedule(tenant, first)  # закончилась — можно снова
    await scheduler.drain()
    assert runs == [first, second, first]


async def test_scheduler_close_cancels_running_summaries() -> None:
    started = asyncio.Event()
    cancelled = False

    async def run(tenant_id: TenantId, conversation_id: UUID) -> bool:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return True

    scheduler = SummaryScheduler(run)
    scheduler.schedule(TenantId(uuid4()), uuid4())
    await started.wait()

    await scheduler.aclose()

    assert cancelled


KEY = {"X-Widget-Key": "wk_shop"}


type UseSummaryLLM = Callable[[FakeLLM], None]


@pytest.fixture
def use_summary_llm(app: FastAPI, db_session: AsyncSession) -> Iterator[UseSummaryLLM]:
    """Модель сводки; сводка в фоне — через сессию теста (транзакция с откатом)."""

    def use(llm: FakeLLM) -> None:
        scheduler = SummaryScheduler(
            lambda tenant_id, conversation_id: _summarize(
                db_session, tenant_id, conversation_id, llm
            )
        )
        app.dependency_overrides[get_summary_scheduler] = lambda: scheduler
        app.state.summary_scheduler = scheduler

    yield use


@pytest.fixture
def agent(app: FastAPI) -> CapturingAgent:
    capturing = CapturingAgent()
    app.dependency_overrides[get_turn_agent] = lambda: capturing
    return capturing


async def _shop(session: AsyncSession, config: dict[str, Any]) -> tuple[TenantId, UUID]:
    tenant_id, conversation_id = await _conversation(session, "shop", config)
    await WidgetKeyRepository(session).add_key(tenant_id, hash_widget_key("wk_shop"), [])
    return tenant_id, conversation_id


async def _send(client: AsyncClient, app: FastAPI, conversation_id: UUID, text: str) -> str:
    """Ход через API; дождаться фоновых сводок. Текст тела стрима."""
    response = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_message_id": str(uuid4()), "input": {"type": "text", "text": text}},
        headers=KEY,
    )
    assert response.status_code == 200
    scheduler: SummaryScheduler = app.state.summary_scheduler
    await scheduler.drain()
    return response.text


async def test_summary_is_saved_after_long_turn_and_next_turn_sees_it(
    app: FastAPI,
    db_client: AsyncClient,
    db_session: AsyncSession,
    use_summary_llm: UseSummaryLLM,
    agent: CapturingAgent,
) -> None:
    tenant_id, conversation_id = await _shop(db_session, _config(SMALL_MEMORY))
    ids = await _add(db_session, tenant_id, conversation_id, "Q1 " + LONG, "A1")
    summary_llm = FakeLLM([FakeReply(text="- клиент спросил Q1")])
    use_summary_llm(summary_llm)

    await _send(db_client, app, conversation_id, "Q2")

    conversation = await ConversationRepository(db_session).find(tenant_id, conversation_id)
    assert conversation is not None
    assert (conversation.summary, conversation.summary_message_id) == (
        "- клиент спросил Q1",
        ids[1],
    )

    await _send(db_client, app, conversation_id, "Q3")

    second = agent.requests[1]
    assert second.history_summary == "- клиент спросил Q1"
    assert [m.content for m in second.history] == ["Q2", "ответ", "Q3"]
    assert len(summary_llm.requests) == 1  # после сводки история короткая


async def test_short_history_does_not_start_summary(
    app: FastAPI,
    db_client: AsyncClient,
    db_session: AsyncSession,
    use_summary_llm: UseSummaryLLM,
    agent: CapturingAgent,
) -> None:
    _, conversation_id = await _shop(db_session, _config(SMALL_MEMORY))
    summary_llm = FakeLLM([])
    use_summary_llm(summary_llm)

    await _send(db_client, app, conversation_id, "Q1")

    assert summary_llm.requests == []


async def test_summary_model_error_does_not_affect_turn(
    app: FastAPI,
    db_client: AsyncClient,
    db_session: AsyncSession,
    use_summary_llm: UseSummaryLLM,
    agent: CapturingAgent,
) -> None:
    tenant_id, conversation_id = await _shop(db_session, _config(SMALL_MEMORY))
    await _add(db_session, tenant_id, conversation_id, "Q1 " + LONG, "A1")
    use_summary_llm(FakeLLM([LLMError("недоступна", retryable=True)]))

    with structlog.testing.capture_logs() as logs:
        body = await _send(db_client, app, conversation_id, "Q2")

    assert '"status":"completed"' in body
    assert any(log["event"] == "summary_failed" for log in logs)
    history = await MessageRepository(db_session).list_for(tenant_id, conversation_id)
    assert [m.content for m in history][-1] == "ответ"
    conversation = await ConversationRepository(db_session).find(tenant_id, conversation_id)
    assert conversation is not None
    assert (conversation.summary, conversation.summary_message_id) == (None, None)
