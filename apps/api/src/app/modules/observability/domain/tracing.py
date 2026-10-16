"""Порт трейсинга LLM (architecture.md §13, ADR-0029): трейс на ход, в нём — вызовы модели
(generation, по одной на попытку) и инструментов. Реализация — Langfuse
(`infrastructure.langfuse_tracer`); без ключей — `NoopTracer`."""

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TurnTraceInfo:
    """Атрибуты трейса хода. `trace_id` — `X-Trace-Id` запроса хода (пусто — новый трейс);
    `prompt_versions` — версии слоёв промпта (Platform, AgentConfig) для каждой generation."""

    trace_id: str
    tenant_id: UUID
    conversation_id: UUID
    turn_id: UUID
    input: dict[str, Any]
    prompt_versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerationInfo:
    """Вызов модели: `input` — системный промпт и сообщения шага, `parameters` — параметры
    запроса (temperature, инструменты). `provider` — провайдер модели (`anthropic`, `openai`,
    …): от него зависят названия корзин токенов, по которым Langfuse считает цену."""

    name: str
    model: str
    input: dict[str, Any]
    parameters: dict[str, Any] = field(default_factory=dict)
    provider: str = ""


@dataclass(frozen=True, slots=True)
class GenerationUsage:
    """Токены попытки. `input_tokens` — весь вход, включая кэш промпта; `cache_read_tokens`
    и `cache_write_tokens` — его доли, прочитанные из кэша и записанные в кэш."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class GenerationTrace(Protocol):
    def first_chunk(self) -> None:
        """Пришёл первый чанк стрима: время до первого токена."""
        ...

    def finish(self, output: dict[str, Any], usage: GenerationUsage) -> None: ...

    def fail(self, error: str, usage: GenerationUsage | None = None) -> None:
        """Попытка закончилась ошибкой или отменой; `usage` — токены, известные до обрыва
        (при отмене хода их нет)."""
        ...


class TurnTrace(Protocol):
    def generation(self, info: GenerationInfo) -> GenerationTrace: ...

    def tool_started(self, call_id: str, name: str) -> None: ...

    def tool_finished(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        output: Any,
        error: str | None,
        duration_ms: int,
    ) -> None: ...

    def finish(self, output: dict[str, Any], error: str | None = None) -> None:
        """Закрыть трейс хода; `error` — ход упал или прерван."""
        ...


class Tracer(Protocol):
    def start_turn(self, info: TurnTraceInfo) -> TurnTrace: ...

    def shutdown(self) -> None:
        """Отправить накопленное и остановиться (при остановке приложения)."""
        ...


class _NoopGeneration:
    def first_chunk(self) -> None:
        pass

    def finish(self, output: dict[str, Any], usage: GenerationUsage) -> None:
        pass

    def fail(self, error: str, usage: GenerationUsage | None = None) -> None:
        pass


class _NoopTurn:
    def generation(self, info: GenerationInfo) -> GenerationTrace:
        return _NoopGeneration()

    def tool_started(self, call_id: str, name: str) -> None:
        pass

    def tool_finished(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        output: Any,
        error: str | None,
        duration_ms: int,
    ) -> None:
        pass

    def finish(self, output: dict[str, Any], error: str | None = None) -> None:
        pass


class NoopTracer:
    """Трейсинг выключен (нет ключей Langfuse, тесты, эвалы)."""

    def start_turn(self, info: TurnTraceInfo) -> TurnTrace:
        return _NoopTurn()

    def shutdown(self) -> None:
        pass
