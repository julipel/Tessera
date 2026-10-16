"""Трейсинг ходов в Langfuse (SDK v4 поверх OpenTelemetry, ADR-0029).

Observation создаются явно (`start_observation` от корня хода), без «текущего» span из
контекста: ход идёт в async-генераторах и отдельной задаче. Атрибуты трейса (сессия, имя,
метаданные) ставятся через `propagate_attributes` на время создания каждого observation —
в v4 они должны быть на каждом observation. Отправка — фоновым экспортёром SDK.

Сбой SDK не ломает ход (REVIEW.md: O-5): каждый вызов observation — под `_guard`, ошибка
логируется `langfuse_trace_failed`. Тексты ошибок (`status_message`) SDK не маскирует —
маскируем сами (O-3, ADR-0032): сообщения валидации содержат значения полей.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import structlog
from langfuse import (
    Langfuse,
    LangfuseAgent,
    LangfuseGeneration,
    LangfuseTool,
    propagate_attributes,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter

from app.modules.observability.domain.pii import mask_pii
from app.modules.observability.domain.tracing import (
    GenerationInfo,
    GenerationTrace,
    NoopTracer,
    Tracer,
    TurnTrace,
    TurnTraceInfo,
)

logger = structlog.get_logger(__name__)

_OTEL_TRACE_ID = re.compile(r"[0-9a-f]{32}")


@contextmanager
def _guard(operation: str) -> Iterator[None]:
    """Глотает Exception SDK (не BaseException: отмена хода проходит). В лог — только тип:
    текст исключения сериализации может содержать данные observation, то есть ПДн."""
    try:
        yield
    except Exception as e:
        logger.error("langfuse_trace_failed", operation=operation, error_type=type(e).__name__)


def _end(observation: Any, operation: str) -> None:
    """Закрыть observation, даже если обновление перед этим упало: незакрытый span не
    уходит в экспорт (корень хода — трейс без корня)."""
    with _guard(operation):
        observation.end()


def _masked(error: str) -> str:
    return str(mask_pii(error))


def langfuse_trace_id(trace_id: str) -> str:
    """id трейса Langfuse (32 hex): наш `X-Trace-Id`, если он такой (его генерирует
    TraceIdMiddleware), иначе — детерминированно из него; пусто — новый."""
    if _OTEL_TRACE_ID.fullmatch(trace_id):
        return trace_id
    return Langfuse.create_trace_id(seed=trace_id) if trace_id else Langfuse.create_trace_id()


class _Generation:
    def __init__(self, observation: LangfuseGeneration) -> None:
        self._observation = observation
        self._started = False

    def first_chunk(self) -> None:
        if not self._started:
            self._started = True
            with _guard("generation.first_chunk"):
                self._observation.update(completion_start_time=datetime.now(UTC))

    def finish(self, output: dict[str, Any], input_tokens: int, output_tokens: int) -> None:
        with _guard("generation.finish"):
            self._observation.update(
                output=output, usage_details={"input": input_tokens, "output": output_tokens}
            )
        _end(self._observation, "generation.end")

    def fail(self, error: str) -> None:
        with _guard("generation.fail"):
            self._observation.update(level="ERROR", status_message=_masked(error))
        _end(self._observation, "generation.end")


class _Turn:
    def __init__(self, client: Langfuse, info: TurnTraceInfo) -> None:
        self._info = info
        self._version = ";".join(f"{k}:{v}" for k, v in info.prompt_versions.items()) or None
        self._attributes: dict[str, Any] = {
            "trace_name": "turn",
            "session_id": str(info.conversation_id),
            "metadata": {
                "tenant_id": str(info.tenant_id),
                "turn_id": str(info.turn_id),
                **({"trace_id": info.trace_id} if info.trace_id else {}),
                **info.prompt_versions,
            },
            "tags": [f"tenant:{info.tenant_id}"],
        }
        self._tools: dict[str, LangfuseTool] = {}
        with propagate_attributes(**self._attributes):
            self._root: LangfuseAgent = client.start_observation(
                trace_context={"trace_id": langfuse_trace_id(info.trace_id)},
                name="turn",
                as_type="agent",
                input=info.input,
                version=self._version,
            )

    def generation(self, info: GenerationInfo) -> GenerationTrace:
        with _guard("generation"), propagate_attributes(**self._attributes):
            return _Generation(
                self._root.start_observation(
                    name=info.name,
                    as_type="generation",
                    model=info.model,
                    input=info.input,
                    model_parameters=info.parameters,
                    version=self._version,
                )
            )
        # Не создалась — попытка модели идёт без своего observation.
        return NoopTracer().start_turn(self._info).generation(info)

    def _tool(self, name: str) -> LangfuseTool:
        with propagate_attributes(**self._attributes):
            return self._root.start_observation(name=name, as_type="tool", version=self._version)

    def tool_started(self, call_id: str, name: str) -> None:
        with _guard("tool_started"):
            self._tools[call_id] = self._tool(name)

    def tool_finished(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        output: Any,
        error: str | None,
        duration_ms: int,
    ) -> None:
        tool = self._tools.pop(call_id, None)
        with _guard("tool_finished"):
            tool = tool or self._tool(name)
            tool.update(input=arguments, output=output, metadata={"duration_ms": duration_ms})
            if error is not None:
                tool.update(level="ERROR", status_message=_masked(error))
        if tool is not None:
            _end(tool, "tool.end")

    def finish(self, output: dict[str, Any], error: str | None = None) -> None:
        for tool in self._tools.values():  # вызовы, прерванные отменой хода
            with _guard("tool_interrupted"):
                tool.update(level="WARNING", status_message="не завершён")
            _end(tool, "tool.end")
        self._tools.clear()
        with _guard("finish"):
            self._root.update(output=output)
            if error is not None:
                self._root.update(level="ERROR", status_message=_masked(error))
        _end(self._root, "turn.end")


class LangfuseTracer:
    """`Tracer` поверх клиента Langfuse. Сбой трейсинга не должен ломать ход: ошибка SDK
    при старте трейса логируется, и ход идёт без трейса; дальше — `_guard` на каждом вызове.
    Сетевые ошибки отправки SDK обрабатывает сам в фоновом экспортёре."""

    def __init__(self, client: Langfuse) -> None:
        self._client = client

    @classmethod
    def create(
        cls,
        public_key: str,
        secret_key: str,
        host: str | None,
        environment: str,
        timeout_s: int = 30,
        span_exporter: SpanExporter | None = None,
    ) -> "LangfuseTracer":
        # Свой TracerProvider: иначе SDK зарегистрирует глобальный провайдер OpenTelemetry.
        # gzip и таймаут отправки больше 5 с по умолчанию: generation несёт весь промпт шага
        # (десятки КБ), и на медленном канале пачка не успевала уйти (P7-03). Отправка — в
        # фоновом потоке SDK, ход она не задерживает. Персональные данные маскируются до
        # отправки (input, output, metadata каждого observation; ADR-0032).
        return cls(
            Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=host,
                environment=environment,
                tracer_provider=TracerProvider(),
                timeout=timeout_s,
                otel_compression="gzip",
                mask=_mask,
                span_exporter=span_exporter,  # в тестах — in-memory вместо отправки
            )
        )

    def start_turn(self, info: TurnTraceInfo) -> TurnTrace:
        try:
            return _Turn(self._client, info)
        except Exception:
            logger.exception("langfuse_trace_failed")
            return NoopTracer().start_turn(info)

    def shutdown(self) -> None:
        self._client.shutdown()


def _mask(*, data: Any, **_: Any) -> Any:
    return mask_pii(data)


def build_tracer(
    public_key: str | None,
    secret_key: str | None,
    host: str | None,
    environment: str,
    timeout_s: int = 30,
) -> Tracer:
    """Трейсер Langfuse, если заданы ключи (публичный и секретный); иначе — выключенный."""
    if not (public_key and secret_key):
        return NoopTracer()
    return LangfuseTracer.create(public_key, secret_key, host, environment, timeout_s)
