"""FakeLLM — LLMClient со сценарием для тестов агентного цикла и chat.

Каждый вызов `stream()` берёт следующий шаг сценария: `FakeReply` проигрывается как стрим,
`LLMError` поднимается до первого чанка. Все запросы сохраняются в `requests`.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

from app.modules.agent.domain.llm import (
    LLMChunk,
    LLMError,
    LLMRequest,
    LLMResponse,
    ResponseCompleted,
    StopReason,
    TextDelta,
    ToolCall,
    ToolCallStarted,
    Usage,
)


@dataclass(frozen=True, slots=True)
class FakeReply:
    """Шаг сценария.

    - `text` режется на куски по словам; `chunks` задаёт куски явно (тогда `text` игнорируется);
    - `delay_s` — пауза перед каждым чанком (таймауты, отмена);
    - `fail_after_chunks` — поднять `error` после стольких текстовых чанков (обрыв стрима).
    """

    text: str = ""
    chunks: tuple[str, ...] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    delay_s: float = 0.0
    usage: Usage | None = None
    fail_after_chunks: int | None = None
    error: LLMError | None = None

    def text_chunks(self) -> list[str]:
        if self.chunks is not None:
            return list(self.chunks)
        return re.findall(r"\S+\s*|\s+", self.text)


type FakeStep = FakeReply | LLMError


class FakeLLMExhaustedError(AssertionError):
    """Модель вызвана больше раз, чем шагов в сценарии."""


class FakeLLM:
    def __init__(self, steps: Iterable[FakeStep] = ()) -> None:
        self._steps = list(steps)
        self.requests: list[LLMRequest] = []

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def add(self, *steps: FakeStep) -> None:
        self._steps.extend(steps)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        self.requests.append(request)
        if not self._steps:
            raise FakeLLMExhaustedError(
                f"FakeLLM: вызов №{len(self.requests)}, а шагов в сценарии больше нет"
            )
        step = self._steps.pop(0)
        if isinstance(step, LLMError):
            raise step

        chunks = step.text_chunks()
        for index, chunk in enumerate(chunks):
            if step.fail_after_chunks == index:
                raise step.error or LLMError("FakeLLM: обрыв стрима", retryable=True)
            await self._pause(step)
            yield TextDelta(chunk)
        for call in step.tool_calls:
            await self._pause(step)
            yield ToolCallStarted(id=call.id, name=call.name)
        await self._pause(step)

        text = "".join(chunks)
        yield ResponseCompleted(
            LLMResponse(
                text=text,
                tool_calls=step.tool_calls,
                stop_reason=StopReason.TOOL_CALLS if step.tool_calls else StopReason.END_TURN,
                usage=step.usage or Usage(input_tokens=0, output_tokens=len(chunks)),
            )
        )

    @staticmethod
    async def _pause(step: FakeReply) -> None:
        if step.delay_s:
            await asyncio.sleep(step.delay_s)
