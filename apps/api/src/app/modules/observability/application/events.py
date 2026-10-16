"""Журнал событий хода в админке: курсор страницы (REVIEW.md: O-8)."""

import base64
import binascii
from datetime import UTC, datetime
from uuid import UUID

from app.modules.observability.domain.events import (
    AgentEventEntry,
    EventCursor,
    InvalidEventCursorError,
)


def cursor_of(entry: AgentEventEntry) -> EventCursor:
    return EventCursor(entry.ts, entry.turn_id, entry.seq)


def encode_cursor(cursor: EventCursor) -> str:
    raw = f"{cursor.ts.isoformat()}|{cursor.turn_id}|{cursor.seq}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> EventCursor:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        ts, turn_id, seq = raw.split("|")
        moment = datetime.fromisoformat(ts)
        if moment.tzinfo is None:
            raise ValueError("время без часового пояса")
        # В UTC: подделанное время у границы календаря (0001-01-01+14:00) — ошибка здесь,
        # а не 500 от Postgres.
        return EventCursor(moment.astimezone(UTC), UUID(turn_id), int(seq))
    except (binascii.Error, UnicodeDecodeError, ValueError, OverflowError) as e:
        raise InvalidEventCursorError("неверный курсор страницы") from e
