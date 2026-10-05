"""LLM-судья: `clarifies`, `max_questions`, `judge` — один вызов модели на ход.

Судья видит предыдущие ходы, текущую реплику, ответ агента, вызванные инструменты, компоненты
с их содержимым (названия, цены, строки таблиц) и слоты, а отвечает JSON. Сбой модели или
неразборчивый ответ — `error` у проверок хода, прогон продолжается.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.contracts import AgentConfig
from app.modules.agent.public import (
    LLMClient,
    LLMRequest,
    Provider,
    ResponseCompleted,
    Usage,
    UserMessage,
)
from evals.checks import JUDGE_CHECKS, CheckResult, CheckStatus, TurnOutcome
from evals.dialogs import Expect

# Меняется вместе с JUDGE_SYSTEM: прогоны с разными версиями судьи сравнивать напрямую нельзя.
JUDGE_PROMPT_VERSION = "3"

JUDGE_SYSTEM = """\
Ты — строгий и беспристрастный оценщик ответов AI-консультанта интернет-магазина.
На входе история диалога, последнюю реплику пользователя и последний ответ консультанта
с тем, что консультант сделал за ход (вызванные инструменты, показанные UI-компоненты
с их содержимым, сохранённые сведения о запросе). Цены и характеристики товаров консультант
показывает карточками и таблицами, а не текстом: учитывай их как часть ответа.
Карточка показывает только название, цену и бейджи, а не все характеристики товара (состав,
ноты, отдушки), и каталога ты не видишь: не ставь провал за то, что характеристику, названную
в ответе, нельзя проверить по карточке.
Оценивай только последний ответ.

Верни только JSON-объект, без текста вокруг:
{"questions": <int>, "clarifies": <bool>, "rubric": {"pass": <bool>, "reason": "<str>"} | null}

- questions — сколько вопросов консультант задаёт пользователю в последнем ответе
  (вопросы о его запросе и предпочтениях; вежливое «могу ещё чем-то помочь?» не считается).
- clarifies — true, если консультант запрашивает у пользователя недостающие сведения
  о запросе, вместо того чтобы сразу дать ответ или подборку, или прежде чем дать их.
  Попутный вопрос после полноценного ответа — false.
- rubric — оценка по рубрике, если она дана, иначе null. pass — true, только если ответ
  выполняет рубрику целиком; reason — одно-два предложения на русском, почему.
"""


class _Rubric(BaseModel):
    passed: bool = Field(alias="pass")
    reason: str = ""


class _Verdict(BaseModel):
    questions: int = Field(ge=0)
    clarifies: bool
    rubric: _Rubric | None = None


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    """Прошлый ход для судьи: реплика пользователя (как в отчёте) и ответ агента."""

    user: str
    assistant: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """Результаты проверок судьи по имени проверки и токены вызова судьи."""

    checks: dict[str, CheckResult] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)


type LLMForProvider = Callable[[Provider], LLMClient]


class Judge:
    """`provider`/`model` не заданы — судит `model.primary` тенанта диалога."""

    def __init__(
        self, llm_for: LLMForProvider, provider: Provider | None = None, model: str | None = None
    ) -> None:
        self._llm_for = llm_for
        self._provider = provider
        self._model = model

    def model_for(self, config: dict[str, Any]) -> tuple[Provider, str]:
        primary = AgentConfig.model_validate(config).model.primary
        return self._provider or primary.provider, self._model or primary.name

    async def judge_turn(
        self,
        config: dict[str, Any],
        transcript: Sequence[TranscriptTurn],
        user: str,
        outcome: TurnOutcome,
        expect: Expect,
    ) -> Verdict:
        wanted = [name for name in JUDGE_CHECKS if name in expect.model_fields_set]
        if not wanted:
            return Verdict()
        usage = Usage()
        try:
            provider, model = self.model_for(config)
            request = LLMRequest(
                model=model,
                system=JUDGE_SYSTEM,
                messages=(UserMessage(render_task(transcript, user, outcome, expect.judge)),),
            )
            text = ""
            async for chunk in self._llm_for(provider).stream(request):
                if isinstance(chunk, ResponseCompleted):
                    text, usage = chunk.response.text, chunk.response.usage
            verdict = parse_verdict(text)
        except Exception as e:
            reason = f"судья: {type(e).__name__}: {e}"
            return Verdict({n: CheckResult(n, CheckStatus.ERROR, reason) for n in wanted}, usage)
        return Verdict({n: _check(n, verdict, expect) for n in wanted}, usage)


def render_task(
    transcript: Sequence[TranscriptTurn], user: str, outcome: TurnOutcome, rubric: str | None
) -> str:
    lines = ["## История диалога", ""]
    for turn in transcript:
        lines += [f"Пользователь: {turn.user}", f"Консультант: {turn.assistant or '—'}", ""]
    if not transcript:
        lines += ["(начало диалога)", ""]
    lines += [
        "## Последняя реплика пользователя",
        "",
        user,
        "",
        "## Последний ответ консультанта",
        "",
        outcome.text or "(пустой ответ)",
        "",
        f"Вызванные инструменты: {', '.join(outcome.tools_called) or 'нет'}",
        f"Показанные UI-компоненты: {', '.join(outcome.components) or 'нет'}",
        *_describe_components(outcome.component_data),
        f"Сохранённые сведения о запросе: {outcome.slots or 'нет'}",
        "",
        "## Рубрика",
        "",
        rubric or "(нет — rubric: null)",
    ]
    return "\n".join(lines)


def _describe_components(components: Sequence[Mapping[str, Any]]) -> list[str]:
    """Содержимое компонентов для судьи: карточки — название, подзаголовок, цена, бейджи;
    таблица сравнения — колонки и строки; прочие — тип и заголовок."""
    lines: list[str] = []
    for component in components:
        kind = component.get("type", "?")
        match kind:
            case "product_card":
                lines.append(f"- product_card: {_describe_card(component)}")
            case "product_carousel":
                title = f" «{component['title']}»" if component.get("title") else ""
                lines.append(f"- product_carousel{title}:")
                lines += [f"  - {_describe_card(item)}" for item in component.get("items", [])]
            case "comparison_table":
                lines.append(f"- comparison_table: {' | '.join(component.get('columns', []))}")
                for row in component.get("rows", []):
                    lines.append(f"  - {row.get('label', '')}: {' | '.join(row.get('values', []))}")
            case _:
                name = component.get("title")
                lines.append(f"- {kind}" + (f": {name}" if name else ""))
    return lines


def _describe_card(card: Mapping[str, Any]) -> str:
    parts = [str(card.get("title", ""))]
    if subtitle := card.get("subtitle"):
        parts.append(str(subtitle))
    if price := card.get("price"):
        parts.append(f"{price.get('amount')} {price.get('currency', '')}".strip())
    if badges := card.get("badges"):
        parts.append(", ".join(badges))
    return " — ".join(parts)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_verdict(text: str) -> _Verdict:
    """JSON-объект из ответа судьи; допускается обёртка в ```json```. ValueError — не JSON
    или не по схеме."""
    if match := _FENCE.search(text):
        text = match.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"в ответе нет JSON-объекта: {text[:200]!r}")
    try:
        return _Verdict.model_validate_json(text[start : end + 1])
    except ValidationError as e:
        raise ValueError(f"ответ не по схеме: {e.error_count()} ошибок: {text[:200]!r}") from e


def _check(name: str, verdict: _Verdict, expect: Expect) -> CheckResult:
    match name:
        case "clarifies":
            ok = verdict.clarifies == expect.clarifies
            actual = "уточняет" if verdict.clarifies else "не уточняет"
            return CheckResult(name, _status(ok), f"{actual}, вопросов: {verdict.questions}")
        case "max_questions":
            limit = expect.max_questions or 0
            ok = verdict.questions <= limit
            return CheckResult(
                name, _status(ok), f"вопросов: {verdict.questions}, не больше {limit}"
            )
        case _:
            if verdict.rubric is None:
                return CheckResult(name, CheckStatus.ERROR, "судья не оценил рубрику")
            return CheckResult(name, _status(verdict.rubric.passed), verdict.rubric.reason)


def _status(ok: bool) -> CheckStatus:
    return CheckStatus.PASSED if ok else CheckStatus.FAILED
