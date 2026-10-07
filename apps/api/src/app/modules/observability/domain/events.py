"""Журнал событий хода (AgentEvent, architecture.md §13): что произошло в ходе и в каком
порядке — для разбора диалогов в админке."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AgentEventEntry:
    """Событие хода. `seq` — порядок в ходе, `trace_id` — запроса, в котором шёл ход
    (`X-Trace-Id`), `payload` — данные события по `type` (docs/contracts.md §7)."""

    conversation_id: UUID
    turn_id: UUID
    trace_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    ts: datetime
