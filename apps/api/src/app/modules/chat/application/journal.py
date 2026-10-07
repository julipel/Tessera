"""Журнал хода (AgentEvent, architecture.md §13): события хода для разбора в админке."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.modules.agent.kernel import (
    AgentEvent,
    ComponentEmitted,
    DialogStateUpdated,
    SuggestionsOffered,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from app.modules.observability.kernel import AgentEventEntry


class TurnJournal:
    """Копит события хода; записывает их TurnStream вместе с ответом.

    Текст — блоком целиком (`text`, на месте начала блока), а не кусками стрима. Результат
    инструмента не дублируется: он в ToolCall ответа (`message_id` из `turn_finished`).
    `turn_finished` — последнее событие, есть у любого записанного хода (и прерванного,
    и неудачного).
    """

    def __init__(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        trace_id: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._conversation_id = conversation_id
        self._turn_id = turn_id
        self._trace_id = trace_id
        self._now = now
        self._entries: list[AgentEventEntry] = []

    @property
    def entries(self) -> tuple[AgentEventEntry, ...]:
        return tuple(self._entries)

    def record(self, type_: str, payload: dict[str, Any]) -> None:
        self._entries.append(
            AgentEventEntry(
                conversation_id=self._conversation_id,
                turn_id=self._turn_id,
                trace_id=self._trace_id,
                seq=len(self._entries) + 1,
                type=type_,
                payload=payload,
                ts=self._now(),
            )
        )

    def record_agent_event(self, event: AgentEvent, block_id: str | None = None) -> None:
        """Событие агента; `block_id` — блок ответа компонента. Куски текста пишет
        TurnStream блоком (`text`)."""
        match event:
            case ToolStarted(tool_call_id=call_id, name=name):
                self.record("tool_started", {"tool_call_id": call_id, "name": name})
            case ToolFinished() as finished:
                self.record("tool_finished", _tool_finished(finished))
            case ComponentEmitted(component=component):
                self.record("component", {"block_id": block_id, "component": component})
            case SuggestionsOffered(items=items):
                self.record("suggestions", {"items": list(items)})
            case DialogStateUpdated(state=state):
                self.record("state_updated", {"state": state.to_dict()})
            case TurnCompleted(finish=finish, usage=usage, steps=steps):
                self.record(
                    "turn_completed",
                    {
                        "finish": finish.value,
                        "steps": steps,
                        "usage": {
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                        },
                    },
                )


def _tool_finished(finished: ToolFinished) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_call_id": finished.tool_call_id,
        "name": finished.name,
        "ok": finished.ok,
        "arguments": finished.arguments,
        "duration_ms": finished.duration_ms,
    }
    if finished.error_code:
        payload["error"] = {"code": finished.error_code, "message": finished.error_message or ""}
    return payload
