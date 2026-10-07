"""Трейсинг ходов в Langfuse (SDK v4 поверх OpenTelemetry, ADR-0029).

Observation создаются явно (`start_observation` от корня хода), без «текущего» span из
контекста: ход идёт в async-генераторах и отдельной задаче. Атрибуты трейса (сессия, имя,
метаданные) ставятся через `propagate_attributes` на время создания каждого observation —
в v4 они должны быть на каждом observation. Отправка — фоновым экспортёром SDK.
"""

import re
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
            self._observation.update(completion_start_time=datetime.now(UTC))

    def finish(self, output: dict[str, Any], input_tokens: int, output_tokens: int) -> None:
        self._observation.update(
            output=output, usage_details={"input": input_tokens, "output": output_tokens}
        )
        self._observation.end()

    def fail(self, error: str) -> None:
        self._observation.update(level="ERROR", status_message=error)
        self._observation.end()


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
        with propagate_attributes(**self._attributes):
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

    def _tool(self, name: str) -> LangfuseTool:
        with propagate_attributes(**self._attributes):
            return self._root.start_observation(name=name, as_type="tool", version=self._version)

    def tool_started(self, call_id: str, name: str) -> None:
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
        tool = self._tools.pop(call_id, None) or self._tool(name)
        tool.update(input=arguments, output=output, metadata={"duration_ms": duration_ms})
        if error is not None:
            tool.update(level="ERROR", status_message=error)
        tool.end()

    def finish(self, output: dict[str, Any], error: str | None = None) -> None:
        for tool in self._tools.values():  # вызовы, прерванные отменой хода
            tool.update(level="WARNING", status_message="не завершён")
            tool.end()
        self._tools.clear()
        self._root.update(output=output)
        if error is not None:
            self._root.update(level="ERROR", status_message=error)
        self._root.end()


class LangfuseTracer:
    """`Tracer` поверх клиента Langfuse. Сбой трейсинга не должен ломать ход: ошибка SDK
    при старте трейса логируется, и ход идёт без трейса. Сетевые ошибки отправки SDK
    обрабатывает сам в фоновом экспортёре."""

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
    ) -> "LangfuseTracer":
        # Свой TracerProvider: иначе SDK зарегистрирует глобальный провайдер OpenTelemetry.
        # gzip и таймаут отправки больше 5 с по умолчанию: generation несёт весь промпт шага
        # (десятки КБ), и на медленном канале пачка не успевала уйти (P7-03). Отправка — в
        # фоновом потоке SDK, ход она не задерживает.
        return cls(
            Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=host,
                environment=environment,
                tracer_provider=TracerProvider(),
                timeout=timeout_s,
                otel_compression="gzip",
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
