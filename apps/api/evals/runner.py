"""Проигрывание эталонного диалога через агента in-process (ADR-0011).

Ход собирает тот же `LoopTurnAgent`, что и chat API; история, DialogState и показанные
компоненты живут в памяти раннера, в БД ничего не пишется.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from app.modules.agent.public import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    DialogStateUpdated,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from app.modules.chat.public import ChatMessage, MessageRole, MessageStatus, TurnRequest
from app.modules.shared.kernel import TenantId
from app.modules.tenants.public import load_tenant_spec
from evals.checks import CheckResult, CheckStatus, TurnOutcome, check_turn, needs_judge
from evals.dialogs import REPO_ROOT, Dialog, describe_input
from evals.judge import Judge, TranscriptTurn

TENANTS_DIR = REPO_ROOT / "config" / "tenants"
# Служебное `action_id` эталона: раннер подставляет действие последнего компонента `confirm`.
CONFIRM_ACTION = "confirm"
# Пространство имён для стабильных id тенанта и конфига эвала (в БД их нет).
_EVAL_NAMESPACE = UUID("6f1d3c52-8a8e-4c55-9a43-0e7f1c2b5e11")


class TurnAgent(Protocol):
    def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]: ...


class RunStatus(StrEnum):
    """Итог хода или диалога: `error` — агент упал или конфиг не загрузился, `skipped` —
    ход не проигрывался после ошибки предыдущего."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(slots=True)
class TurnResult:
    index: int
    input: dict[str, Any]
    outcome: TurnOutcome = field(default_factory=TurnOutcome)
    checks: list[CheckResult] = field(default_factory=list)
    finish: str | None = None
    # Диагностика хода: вызовы инструментов по шагам модели (ошибка — `имя!код`),
    # число вызовов модели и активный сценарий после хода.
    tool_steps: list[list[str]] = field(default_factory=list)
    steps: int = 0
    scenario: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    duration_ms: int = 0
    error: str | None = None
    skipped: bool = False

    @property
    def status(self) -> RunStatus:
        if self.skipped:
            return RunStatus.SKIPPED
        if self.error is not None:
            return RunStatus.ERROR
        if any(c.status in (CheckStatus.FAILED, CheckStatus.ERROR) for c in self.checks):
            return RunStatus.FAILED
        return RunStatus.PASSED


@dataclass(slots=True)
class DialogResult:
    dialog_id: str
    tenant: str
    tags: list[str]
    turns: list[TurnResult] = field(default_factory=list)
    error: str | None = None

    @property
    def status(self) -> RunStatus:
        statuses = {turn.status for turn in self.turns}
        if self.error is not None or RunStatus.ERROR in statuses:
            return RunStatus.ERROR
        if RunStatus.FAILED in statuses:
            return RunStatus.FAILED
        return RunStatus.PASSED

    @property
    def checks(self) -> list[CheckResult]:
        return [check for turn in self.turns for check in turn.checks]


class TenantConfigs:
    """AgentConfig тенанта из `config/tenants/<slug>.yaml` — рабочая копия, без seed в БД."""

    def __init__(self, directory: Path = TENANTS_DIR) -> None:
        self._directory = directory
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, slug: str) -> dict[str, Any]:
        """FileNotFoundError / InvalidTenantSpecError / ValueError — конфиг не загрузить."""
        if (config := self._cache.get(slug)) is not None:
            return config
        path = self._directory / f"{slug}.yaml"
        spec = load_tenant_spec(path.read_text(encoding="utf-8"))
        if spec.tenant.slug != slug:
            raise ValueError(f"{path.name}: slug тенанта {spec.tenant.slug!r}, а не {slug!r}")
        config = self._cache[slug] = spec.config_json()
        return config


class DialogRunner:
    """`judge` — LLM-судья; None — его проверки `skipped`. `turn_timeout_s` — страховка от
    зависшего хода; лимиты хода задаёт AgentConfig. `tenant_ids` — id тенантов из БД по slug
    (ADR-0011 п.5); тенанту не из словаря — фиксированный id, его данных в БД нет."""

    def __init__(
        self,
        agent: TurnAgent,
        configs: TenantConfigs,
        judge: Judge | None = None,
        turn_timeout_s: float = 180,
        tenant_ids: Mapping[str, TenantId] | None = None,
    ) -> None:
        self._agent = agent
        self._configs = configs
        self._judge = judge
        self._turn_timeout_s = turn_timeout_s
        self._tenant_ids = dict(tenant_ids or {})

    async def run(self, dialog: Dialog) -> DialogResult:
        result = DialogResult(dialog.id, dialog.tenant, list(dialog.tags))
        try:
            config = self._configs.get(dialog.tenant)
        except Exception as e:
            result.error = f"конфиг тенанта {dialog.tenant!r}: {e}"
            return result

        tenant_id = self._tenant_ids.get(dialog.tenant) or TenantId(
            uuid5(_EVAL_NAMESPACE, f"tenant:{dialog.tenant}")
        )
        conversation = _Conversation(tenant_id=tenant_id, conversation_id=uuid4())
        transcript: list[TranscriptTurn] = []
        failed = False
        for index, turn in enumerate(dialog.turns, start=1):
            user_input = conversation.resolve_input(turn.user_input())
            turn_result = TurnResult(index=index, input=user_input)
            result.turns.append(turn_result)
            if failed:
                turn_result.skipped = True
                continue
            await self._play(conversation, config, turn_result)
            if turn_result.error is not None:
                failed = True
                continue
            judged: dict[str, CheckResult] = {}
            user = describe_input(user_input)
            if self._judge is not None and needs_judge(turn.expect):
                verdict = await self._judge.judge_turn(
                    config, transcript, user, turn_result.outcome, turn.expect
                )
                judged = verdict.checks
                turn_result.judge_input_tokens = verdict.usage.input_tokens
                turn_result.judge_output_tokens = verdict.usage.output_tokens
            turn_result.checks = check_turn(turn.expect, turn_result.outcome, judged)
            transcript.append(TranscriptTurn(user, turn_result.outcome.text))
        return result

    async def _play(
        self, conversation: "_Conversation", config: dict[str, Any], result: TurnResult
    ) -> None:
        conversation.add(MessageRole.USER, _user_text(result.input), result.input)
        request = TurnRequest(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.conversation_id,
            agent_config_id=uuid5(_EVAL_NAMESPACE, f"config:{conversation.tenant_id}"),
            turn_id=uuid4(),
            input=result.input,
            agent_config=config,
            history=tuple(conversation.history),
            dialog_state=conversation.state,
        )
        collector = _TurnCollector()
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._turn_timeout_s):
                async for event in self._agent.run_turn(request):
                    collector.add(event)
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        result.duration_ms = int((time.monotonic() - started) * 1000)

        text = collector.text()
        if collector.state is not None:
            conversation.state = collector.state
        conversation.components.extend(collector.components)
        conversation.add(MessageRole.ASSISTANT, text)
        result.outcome = TurnOutcome(
            text=text,
            tools_called=tuple(collector.tools),
            components=tuple(c.get("type", "?") for c in collector.components),
            slots=dict(conversation.state.get("slots") or {}),
        )
        result.finish = collector.finish
        result.tool_steps = collector.tool_steps
        result.steps = collector.steps
        result.scenario = conversation.state.get("active_scenario")
        result.input_tokens = collector.input_tokens
        result.output_tokens = collector.output_tokens


class MemoryLeads:
    """Хранилище заявок `create_lead` для эвалов (порт tools `LeadStore`): прогоны не пишут
    заявки в БД тенанта, созданные видны в `created`."""

    def __init__(self) -> None:
        self.created: list[tuple[TenantId, UUID, str, dict[str, str]]] = []

    async def create(
        self, tenant_id: TenantId, conversation_id: UUID, form_key: str, fields: Mapping[str, str]
    ) -> UUID:
        self.created.append((tenant_id, conversation_id, form_key, dict(fields)))
        return uuid4()


async def run_dialogs(
    runner: DialogRunner, dialogs: Iterable[Dialog], concurrency: int = 4
) -> list[DialogResult]:
    """Диалоги параллельно (не больше `concurrency`), ходы внутри диалога — по очереди.
    Результаты — в порядке `dialogs`."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_one(dialog: Dialog) -> DialogResult:
        async with semaphore:
            return await runner.run(dialog)

    return list(await asyncio.gather(*(run_one(d) for d in dialogs)))


@dataclass(slots=True)
class _Conversation:
    tenant_id: TenantId
    conversation_id: UUID
    history: list[ChatMessage] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    components: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self, role: MessageRole, content: str, user_input: dict[str, Any] | None = None
    ) -> None:
        self.history.append(
            ChatMessage(
                id=uuid4(),
                tenant_id=self.tenant_id,
                conversation_id=self.conversation_id,
                role=role,
                status=MessageStatus.COMPLETED,
                content=content,
                input=user_input,
                blocks=[],
                client_message_id=None,
                created_at=datetime.now(UTC),
            )
        )

    def resolve_input(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Реальные id вместо ключей эталона (evals/README.md, «Ввод не текстом»):
        `form_id` — из последней показанной формы, `action_id: confirm` — действие
        подтверждения последнего `confirm`. Нет такого компонента — ввод как в эталоне."""
        match user_input.get("type"):
            case "form_submit":
                form = self._last("form")
                if form is not None and "form_id" in form:
                    return {**user_input, "form_id": form["form_id"]}
            case "action" if user_input.get("action_id") == CONFIRM_ACTION:
                confirm = self._last("confirm")
                action = (confirm or {}).get("confirm_action") or {}
                if "action_id" in action:
                    return {
                        **user_input,
                        "action_id": action["action_id"],
                        "payload": action.get("payload") or user_input.get("payload") or {},
                    }
        return user_input

    def _last(self, kind: str) -> dict[str, Any] | None:
        return next((c for c in reversed(self.components) if c.get("type") == kind), None)


class _TurnCollector:
    """События хода → ответ как его записывает chat: текстовые блоки, разделённые
    инструментами и компонентами, склеиваются через пустую строку."""

    def __init__(self) -> None:
        self._blocks: list[str] = []
        self._open = False
        self.tools: list[str] = []
        # Шаг модели — пакет вызовов: все ToolStarted шага приходят раньше его ToolFinished,
        # поэтому ToolStarted после ToolFinished открывает новый шаг.
        self.tool_steps: list[list[str]] = []
        self._step_done = True
        self.steps = 0
        self.components: list[dict[str, Any]] = []
        self.state: dict[str, Any] | None = None
        self.finish: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, event: AgentEvent) -> None:
        match event:
            case AnswerDelta(text=text) if text:
                if not self._open:
                    self._blocks.append("")
                    self._open = True
                self._blocks[-1] += text
            case ToolStarted():
                self._open = False
                if self._step_done:
                    self.tool_steps.append([])
                    self._step_done = False
            case ToolFinished(name=name, ok=ok, error_code=code):
                if name not in self.tools:
                    self.tools.append(name)
                if not self.tool_steps:
                    self.tool_steps.append([])  # подтверждённый вызов — без шага модели
                self.tool_steps[-1].append(name if ok else f"{name}!{code}")
                self._step_done = True
            case ComponentEmitted(component=component):
                self._open = False
                self.components.append(component)
            case DialogStateUpdated(state=state):
                self.state = state.to_dict()
            case TurnCompleted(finish=finish, usage=usage, steps=steps):
                self.finish = finish.value
                self.steps = steps
                self.input_tokens = usage.input_tokens
                self.output_tokens = usage.output_tokens

    def text(self) -> str:
        return "\n\n".join(self._blocks)


def _user_text(user_input: dict[str, Any]) -> str:
    """Как chat хранит сообщение пользователя: текст только у текстового ввода."""
    return str(user_input.get("text", "")) if user_input.get("type") == "text" else ""
