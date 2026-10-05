"""Раннер эвалов (P3-01a, ADR-0011): формат диалогов, проверки, проигрывание, отчёт."""

import json
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent.public import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    DialogStateUpdated,
    FakeLLM,
    FakeReply,
    FinishReason,
    LLMError,
    ToolCall,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    Usage,
    UserMessage,
)
from app.modules.chat.public import TurnRequest, builtin_turn_agent
from app.modules.memory.public import DialogState
from app.modules.shared.public import TenantId
from app.modules.tenants.public import SqlTenantDirectory
from app.settings import Settings
from evals.checks import CheckResult, CheckStatus, TurnOutcome, check_turn
from evals.dialogs import Dialog, Expect, InvalidDialogError, load_dialogs, select_dialogs
from evals.judge import Judge, TranscriptTurn, parse_verdict, render_task
from evals.report import RunInfo, render_markdown, render_summary, totals, write_report
from evals.runner import DialogRunner, RunStatus, TenantConfigs, run_dialogs
from evals.tenant_data import connect_tenant_data, resolve_tenant_ids

TENANT_YAML = """
tenant: { slug: eval-shop, name: Eval Shop }
agent_config:
  assistant: { name: A, greeting: "Привет!", fallback_message: "Не вышло." }
  model:
    primary: { provider: openai, name: test-model }
  limits: {}
  prompt:
    tenant: "Ты — консультант."
    scenarios:
      - key: skincare
        description: Уход за кожей
        instructions: Выясни тип кожи.
        slots:
          skin_type: { type: string, enum: [dry, oily] }
          concerns: { type: array, items: { type: string } }
  tools: { builtin: [update_dialog_state] }
"""


@pytest.fixture
def configs(tmp_path: Path) -> TenantConfigs:
    (tmp_path / "eval-shop.yaml").write_text(TENANT_YAML, encoding="utf-8")
    return TenantConfigs(tmp_path)


def dialog(*turns: dict[str, Any], tenant: str = "eval-shop") -> Dialog:
    return Dialog.model_validate({"id": "d1", "tenant": tenant, "turns": list(turns)})


def call(name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=f"call_{name}", name=name, arguments=arguments, raw_arguments=json.dumps(arguments)
    )


# --- формат диалогов ---


def test_all_reference_dialogs_load() -> None:
    dialogs = load_dialogs()

    assert len(dialogs) >= 15
    assert all(d.tenant == "demo-beauty" for d in dialogs)


def test_turn_needs_exactly_one_of_user_and_input() -> None:
    with pytest.raises(ValueError, match="ровно одно"):
        dialog({"expect": {}})
    with pytest.raises(ValueError, match="ровно одно"):
        dialog({"user": "Привет", "input": {"type": "text", "text": "Привет"}})


def test_unknown_expect_key_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "id: x\ntenant: t\nturns:\n  - user: Привет\n    expect: { tool_called: [a] }\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidDialogError, match=r"bad\.yaml"):
        load_dialogs(tmp_path)


def test_filter_by_id_substring_or_tag() -> None:
    a = Dialog.model_validate(
        {"id": "gift_1", "tenant": "t", "tags": ["lead"], "turns": [{"user": "x"}]}
    )
    b = Dialog.model_validate({"id": "faq_2", "tenant": "t", "turns": [{"user": "x"}]})

    assert select_dialogs([a, b], "faq") == [b]
    assert select_dialogs([a, b], "lead") == [a]
    assert select_dialogs([a, b], None) == [a, b]


# --- проверки ---


def statuses(results: Iterable[CheckResult]) -> dict[str, CheckStatus]:
    return {r.name: r.status for r in results}


def test_checks_pass_on_matching_outcome() -> None:
    expect = Expect.model_validate(
        {
            "tools_called": ["search_catalog"],
            "tools_not_called": ["create_lead"],
            "components": ["product_carousel"],
            "state_contains": {"skin_type": "dry", "concerns": ["dullness"], "budget": 3000},
            "must_contain": ["КРЕМ"],
            "must_not_contain": ["не могу помочь"],
        }
    )
    outcome = TurnOutcome(
        text="Вот крем для сухой кожи",
        tools_called=("update_dialog_state", "search_catalog"),
        components=("product_carousel",),
        slots={"skin_type": "dry", "concerns": ["dullness", "acne"], "budget": 3000.0},
    )

    assert set(statuses(check_turn(expect, outcome)).values()) == {CheckStatus.PASSED}


def test_checks_fail_with_details() -> None:
    expect = Expect.model_validate(
        {
            "tools_called": ["search_catalog"],
            "tools_not_called": ["create_lead"],
            "components": ["form"],
            "state_contains": {"skin_type": "dry", "concerns": ["dullness"], "budget": 1},
            "must_contain": ["крем"],
            "must_not_contain": ["Скидка"],
        }
    )
    outcome = TurnOutcome(
        text="Скидка 50%!",
        tools_called=("create_lead",),
        slots={"skin_type": "oily", "concerns": "dullness"},
    )

    results = {r.name: r for r in check_turn(expect, outcome)}

    assert {r.status for r in results.values()} == {CheckStatus.FAILED}
    assert results["tools_called"].detail == "не вызваны: search_catalog"
    assert results["tools_not_called"].detail == "вызваны: create_lead"
    assert results["components"].detail == "нет компонентов: form"
    assert results["state_contains"].detail == (
        "skin_type: ожидалось 'dry', есть 'oily'; "
        "concerns: ожидалось ['dullness'], есть 'dullness'; нет слота budget"
    )
    assert results["must_contain"].detail == "нет в ответе: 'крем'"
    assert results["must_not_contain"].detail == "есть в ответе: 'Скидка'"


def test_judge_checks_are_skipped_and_unset_checks_absent() -> None:
    expect = Expect.model_validate({"clarifies": False, "max_questions": 1, "judge": "Вежлив"})

    assert statuses(check_turn(expect, TurnOutcome())) == {
        "clarifies": CheckStatus.SKIPPED,
        "max_questions": CheckStatus.SKIPPED,
        "judge": CheckStatus.SKIPPED,
    }
    assert check_turn(Expect(), TurnOutcome()) == []


# --- проигрывание через агента ---


async def test_dialog_runs_through_loop_agent_and_keeps_history(configs: TenantConfigs) -> None:
    llm = FakeLLM(
        [
            FakeReply(text="Какой у вас тип кожи?"),
            FakeReply(tool_calls=(call("update_dialog_state", {"slots": {"skin_type": "dry"}}),)),
            FakeReply(text="Для сухой кожи подойдёт крем с керамидами."),
        ]
    )
    runner = DialogRunner(builtin_turn_agent(lambda provider: llm), configs)

    result = await runner.run(
        dialog(
            {"user": "Посоветуйте крем", "expect": {"tools_not_called": ["update_dialog_state"]}},
            {
                "user": "Кожа сухая",
                "expect": {
                    "tools_called": ["update_dialog_state"],
                    "state_contains": {"skin_type": "dry"},
                    "must_contain": ["керамид"],
                    "judge": "Объясняет выбор",
                },
            },
        )
    )

    assert result.status is RunStatus.PASSED
    first, second = result.turns
    assert first.outcome.text == "Какой у вас тип кожи?"
    assert second.outcome.slots == {"skin_type": "dry"}
    assert second.finish == FinishReason.ANSWERED.value
    assert statuses(second.checks)["judge"] is CheckStatus.SKIPPED
    # Второй ход видит историю первого.
    assert llm.requests[1].messages[0] == UserMessage("Посоветуйте крем")
    assert len(llm.requests[1].messages) == 3


class ScriptedAgent:
    """Агент-заглушка: события на каждый ход и запросы, с которыми его вызвали."""

    def __init__(self, *turns: list[AgentEvent] | Exception) -> None:
        self._turns = list(turns)
        self.requests: list[TurnRequest] = []

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        self.requests.append(request)
        step = self._turns.pop(0)
        if isinstance(step, Exception):
            raise step
        for event in step:
            yield event


def done(input_tokens: int = 10, output_tokens: int = 5) -> TurnCompleted:
    return TurnCompleted(FinishReason.ANSWERED, Usage(input_tokens, output_tokens), steps=1)


RUN_INFO = RunInfo(
    started_at=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
    finished_at=datetime(2026, 10, 3, 12, 2, tzinfo=UTC),
    platform_prompt_version="1",
    judge_prompt_version="1",
    models={"eval-shop": "openai/test-model"},
)

FORM = {"type": "form", "form_id": "f_42", "title": "Контакт", "fields": [], "submit_label": "Ок"}
CONFIRM = {
    "type": "confirm",
    "confirm_id": "cf_7",
    "text": "Создать заявку?",
    "confirm_action": {"action_id": "confirm_lead", "label": "Да", "payload": {"lead": 1}},
    "cancel_action": {"action_id": "cancel_lead", "label": "Нет"},
}


async def test_form_and_confirm_ids_come_from_shown_components(configs: TenantConfigs) -> None:
    agent = ScriptedAgent(
        [AnswerDelta("Оставьте контакт"), ComponentEmitted(FORM), done()],
        [ComponentEmitted(CONFIRM), AnswerDelta("Проверьте данные"), done()],
        [AnswerDelta("Заявка создана"), done()],
    )

    result = await DialogRunner(agent, configs).run(
        dialog(
            {"user": "Хочу консультацию", "expect": {"components": ["form"]}},
            {
                "input": {"type": "form_submit", "form_id": "consultation", "values": {"n": "А"}},
                "expect": {"components": ["confirm"]},
            },
            {"input": {"type": "action", "action_id": "confirm", "payload": {}}},
        )
    )

    assert result.status is RunStatus.PASSED
    assert agent.requests[1].input["form_id"] == "f_42"
    assert agent.requests[2].input == {
        "type": "action",
        "action_id": "confirm_lead",
        "payload": {"lead": 1},
    }
    assert result.turns[2].input["action_id"] == "confirm_lead"
    # Ответ — как его хранит chat: текстовые блоки до и после компонента.
    assert result.turns[0].outcome.text == "Оставьте контакт"
    assert result.turns[1].outcome.components == ("confirm",)


async def test_agent_error_marks_turn_and_skips_rest(configs: TenantConfigs) -> None:
    agent = ScriptedAgent(
        [AnswerDelta("Привет"), done()], LLMError("модель недоступна", retryable=True)
    )

    result = await DialogRunner(agent, configs).run(
        dialog({"user": "1"}, {"user": "2", "expect": {"must_contain": ["x"]}}, {"user": "3"})
    )

    assert [t.status for t in result.turns] == [
        RunStatus.PASSED,
        RunStatus.ERROR,
        RunStatus.SKIPPED,
    ]
    assert result.turns[1].error == "LLMError: модель недоступна"
    assert result.turns[1].checks == []
    assert result.status is RunStatus.ERROR
    assert len(agent.requests) == 2


async def test_unknown_tenant_is_dialog_error(configs: TenantConfigs) -> None:
    result = await DialogRunner(ScriptedAgent(), configs).run(
        dialog({"user": "Привет"}, tenant="nope")
    )

    assert result.status is RunStatus.ERROR
    assert result.error is not None and "'nope'" in result.error
    assert result.turns == []


async def test_tenant_id_from_db_when_known(configs: TenantConfigs) -> None:
    tenant_id = TenantId(uuid4())
    agent = ScriptedAgent([AnswerDelta("ok"), done()], [AnswerDelta("ok"), done()])

    await DialogRunner(agent, configs, tenant_ids={"eval-shop": tenant_id}).run(
        dialog({"user": "Привет"})
    )
    await DialogRunner(agent, configs, tenant_ids={"other": tenant_id}).run(
        dialog({"user": "Привет"})
    )

    assert agent.requests[0].tenant_id == tenant_id
    assert agent.requests[1].tenant_id != tenant_id


async def test_resolve_tenant_ids_from_db(db_session: AsyncSession) -> None:
    directory = SqlTenantDirectory(db_session)
    tenant = await directory.create("eval-shop", "Eval Shop")

    assert await resolve_tenant_ids(directory, ["eval-shop", "missing"]) == {"eval-shop": tenant.id}


async def test_unreachable_db_disables_tenant_data() -> None:
    # Немаршрутизируемый адрес: подключение висит — срабатывает таймаут.
    settings = Settings(database_url="postgresql+asyncpg://u:p@10.255.255.1:5432/x")

    data = await connect_tenant_data(settings, ["eval-shop"], timeout_s=0.2)

    assert data.ids == {} and data.catalog is None
    assert data.error is not None and "БД недоступна" in data.error
    await data.aclose()


async def test_run_dialogs_keeps_order(configs: TenantConfigs) -> None:
    answer: list[AgentEvent] = [AnswerDelta("ok"), done()]
    agent = ScriptedAgent(answer, answer, answer)
    dialogs = [
        Dialog.model_validate({"id": f"d{i}", "tenant": "eval-shop", "turns": [{"user": "x"}]})
        for i in range(3)
    ]

    results = await run_dialogs(DialogRunner(agent, configs), dialogs, concurrency=2)

    assert [r.dialog_id for r in results] == ["d0", "d1", "d2"]


# --- отчёт ---


async def test_turn_records_tool_steps_and_scenario(configs: TenantConfigs) -> None:
    def started(call_id: str, name: str) -> ToolStarted:
        return ToolStarted(call_id, name)

    def finished(call_id: str, name: str, code: Any = None) -> ToolFinished:
        return ToolFinished(call_id, name, ok=code is None, error_code=code)

    agent = ScriptedAgent(
        [
            started("c1", "update_dialog_state"),
            started("c2", "search_catalog"),
            finished("c1", "update_dialog_state"),
            finished("c2", "search_catalog", "validation_error"),
            DialogStateUpdated(DialogState(active_scenario="skincare")),
            started("c3", "search_catalog"),
            finished("c3", "search_catalog"),
            AnswerDelta("Вот варианты."),
            TurnCompleted(FinishReason.ANSWERED, Usage(10, 5), steps=3),
        ]
    )

    result = await DialogRunner(agent, configs).run(dialog({"user": "Крем"}))

    [turn] = result.turns
    assert turn.tool_steps == [
        ["update_dialog_state", "search_catalog!validation_error"],
        ["search_catalog"],
    ]
    assert turn.steps == 3
    assert turn.scenario == "skincare"
    markdown = render_markdown([result], RUN_INFO)
    assert (
        "Шаги модели: 3 · по шагам: update_dialog_state, search_catalog!validation_error"
        " → search_catalog"
    ) in markdown
    assert "Сценарий: skincare" in markdown


async def test_report_markdown_json_and_summary(configs: TenantConfigs, tmp_path: Path) -> None:
    agent = ScriptedAgent(
        [AnswerDelta("Ответ без поиска"), done(100, 20)],
        [AnswerDelta("Ок"), done(50, 10)],
    )
    runner = DialogRunner(agent, configs)
    failed = await runner.run(
        dialog({"user": "Найди крем", "expect": {"tools_called": ["search_catalog"]}})
    )
    passed = await runner.run(dialog({"user": "Спасибо", "expect": {"judge": "Вежлив"}}))
    passed.dialog_id = "d2"
    results = [failed, passed]
    info = RUN_INFO

    report = write_report(results, info, tmp_path / "reports")
    markdown = render_markdown(results, info)

    assert report == tmp_path / "reports" / "20261003-120000.md"
    assert report.read_text(encoding="utf-8") == markdown
    assert "| [d1](#d1) | ❌ | 0/1 | tools_called |" in markdown
    assert "- ❌ `tools_called` — не вызваны: search_catalog" in markdown
    assert "- ⏭ `judge` — судья выключен" in markdown
    assert "Токены агента: вход 150, выход 30" in markdown
    assert "eval-shop — openai/test-model" in markdown
    assert "- Судья: выключен (промпт v1)" in markdown

    data = json.loads((tmp_path / "reports" / "20261003-120000.json").read_text("utf-8"))
    assert [d["status"] for d in data["dialogs"]] == ["failed", "passed"]
    assert data["dialogs"][0]["turns"][0]["checks"] == [
        {"name": "tools_called", "status": "failed", "detail": "не вызваны: search_catalog"}
    ]

    summary = render_summary(results, report)
    assert "❌ d1  0/1" in summary
    assert str(report) in summary
    assert not totals(results).ok
    assert totals([passed]).ok


# --- LLM-судья ---


def judge_reply(questions: int, clarifies: bool, rubric: dict[str, Any] | None) -> FakeReply:
    verdict = {"questions": questions, "clarifies": clarifies, "rubric": rubric}
    return FakeReply(text=json.dumps(verdict, ensure_ascii=False), usage=Usage(300, 40))


def judge_of(llm: FakeLLM) -> Judge:
    return Judge(lambda provider: llm)


async def test_judge_verdict_becomes_checks(configs: TenantConfigs) -> None:
    llm = FakeLLM([judge_reply(2, True, {"pass": False, "reason": "Не опирается на атрибуты"})])
    expect = Expect.model_validate({"clarifies": True, "max_questions": 1, "judge": "Атрибуты"})

    verdict = await judge_of(llm).judge_turn(
        configs.get("eval-shop"),
        [TranscriptTurn("Посоветуйте крем", "Какой тип кожи?")],
        "Сухая",
        TurnOutcome(text="Вот крем", tools_called=("search_catalog",), slots={"skin": "dry"}),
        expect,
    )

    assert {n: (c.status, c.detail) for n, c in verdict.checks.items()} == {
        "clarifies": (CheckStatus.PASSED, "уточняет, вопросов: 2"),
        "max_questions": (CheckStatus.FAILED, "вопросов: 2, не больше 1"),
        "judge": (CheckStatus.FAILED, "Не опирается на атрибуты"),
    }
    assert verdict.usage == Usage(300, 40)
    [request] = llm.requests
    assert request.model == "test-model"
    assert request.tools == ()
    [task] = request.messages
    assert isinstance(task, UserMessage)
    for part in ("Посоветуйте крем", "Какой тип кожи?", "Сухая", "Вот крем", "search_catalog"):
        assert part in task.text
    assert task.text.rstrip().endswith("Атрибуты")


def test_judge_task_shows_component_contents() -> None:
    card = {
        "type": "product_card",
        "entity_id": "1",
        "title": "Сыворотка",
        "price": {"amount": 2490, "currency": "RUB"},
    }
    table = {
        "type": "comparison_table",
        "columns": ["А", "Б"],
        "rows": [{"label": "Текстура", "values": ["гель", "крем"]}],
    }
    carousel = {"type": "product_carousel", "title": "Подборка", "items": [card]}
    outcome = TurnOutcome(
        components=("product_carousel", "comparison_table", "form"),
        component_data=(carousel, table, {"type": "form", "title": "Консультация"}),
    )

    task = render_task([], "Сравните", outcome, None)

    assert "- product_carousel «Подборка»:\n  - Сыворотка — 2490 RUB" in task
    assert "- comparison_table: А | Б\n  - Текстура: гель | крем" in task
    assert "- form: Консультация" in task


async def test_judge_model_override(configs: TenantConfigs) -> None:
    providers: list[str] = []
    llm = FakeLLM([judge_reply(0, False, None)])

    def llm_for(provider: Any) -> FakeLLM:
        providers.append(provider)
        return llm

    judge = Judge(llm_for, provider="anthropic", model="judge-model")
    verdict = await judge.judge_turn(
        configs.get("eval-shop"), [], "Привет", TurnOutcome(), Expect(clarifies=False)
    )

    assert providers == ["anthropic"]
    assert llm.requests[0].model == "judge-model"
    assert verdict.checks["clarifies"].status is CheckStatus.PASSED


def test_parse_verdict_accepts_code_fence_and_rejects_garbage() -> None:
    fenced = 'Вот оценка:\n```json\n{"questions": 1, "clarifies": true, "rubric": null}\n```'

    assert parse_verdict(fenced).questions == 1
    with pytest.raises(ValueError, match="нет JSON"):
        parse_verdict("всё хорошо")
    with pytest.raises(ValueError, match="не по схеме"):
        parse_verdict('{"questions": -1, "clarifies": "да"}')


@pytest.mark.parametrize(
    "step",
    [FakeReply(text="не JSON"), LLMError("429", retryable=True)],
    ids=["invalid_json", "llm_error"],
)
async def test_judge_failure_is_error_on_its_checks(
    configs: TenantConfigs, step: FakeReply | LLMError
) -> None:
    expect = Expect.model_validate({"clarifies": False, "judge": "Вежлив", "must_contain": ["ок"]})

    verdict = await judge_of(FakeLLM([step])).judge_turn(
        configs.get("eval-shop"), [], "Привет", TurnOutcome(text="ок"), expect
    )

    assert set(verdict.checks) == {"clarifies", "judge"}
    assert {c.status for c in verdict.checks.values()} == {CheckStatus.ERROR}
    assert all(c.detail.startswith("судья: ") for c in verdict.checks.values())


async def test_judge_missing_rubric_is_error(configs: TenantConfigs) -> None:
    verdict = await judge_of(FakeLLM([judge_reply(0, False, None)])).judge_turn(
        configs.get("eval-shop"), [], "Привет", TurnOutcome(), Expect(judge="Вежлив")
    )

    assert verdict.checks["judge"] == CheckResult(
        "judge", CheckStatus.ERROR, "судья не оценил рубрику"
    )


async def test_runner_calls_judge_only_for_turns_with_judge_checks(
    configs: TenantConfigs,
) -> None:
    agent = ScriptedAgent(
        [AnswerDelta("Какой тип кожи?"), done()],
        [AnswerDelta("Вот крем"), done()],
        [AnswerDelta("Пожалуйста"), done()],
    )
    judge_llm = FakeLLM(
        [
            judge_reply(1, True, None),
            judge_reply(0, False, {"pass": True, "reason": "Помнит тип кожи"}),
        ]
    )

    result = await DialogRunner(agent, configs, judge_of(judge_llm)).run(
        dialog(
            {"user": "Крем", "expect": {"clarifies": True, "max_questions": 1}},
            {"user": "Сухая", "expect": {"clarifies": False, "judge": "Помнит"}},
            {"user": "Спасибо", "expect": {"must_contain": ["пожалуйста"]}},
        )
    )

    assert result.status is RunStatus.PASSED
    assert len(judge_llm.requests) == 2
    second = judge_llm.requests[1].messages[0]
    assert isinstance(second, UserMessage)
    assert "Пользователь: Крем" in second.text
    assert "Консультант: Какой тип кожи?" in second.text
    assert result.turns[1].checks[-1] == CheckResult("judge", CheckStatus.PASSED, "Помнит тип кожи")
    assert result.turns[1].judge_input_tokens == 300
    assert result.turns[2].judge_input_tokens == 0
