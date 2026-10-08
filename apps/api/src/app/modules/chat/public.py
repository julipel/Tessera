"""Публичный интерфейс модуля chat — единственная точка входа для других модулей.

`LoopTurnAgent` и типы запроса хода экспортируются для раннера эвалов (ADR-0011).
"""

from app.modules.chat.api.router import router
from app.modules.chat.api.summaries import background_summary
from app.modules.chat.application.summaries import SummaryScheduler
from app.modules.chat.application.turns import TurnRegistry
from app.modules.chat.domain.entities import ChatMessage, MessageRole, MessageStatus, TurnRequest
from app.modules.chat.infrastructure.loop_agent import LoopTurnAgent, builtin_turn_agent
from app.modules.chat.infrastructure.models import (
    ConversationRecord,
    MessageRecord,
    ToolCallRecord,
)
from app.modules.chat.infrastructure.turn_directory import RedisTurnDirectory

__all__ = [
    "ChatMessage",
    "ConversationRecord",
    "LoopTurnAgent",
    "MessageRecord",
    "MessageRole",
    "MessageStatus",
    "RedisTurnDirectory",
    "SummaryScheduler",
    "ToolCallRecord",
    "TurnRegistry",
    "TurnRequest",
    "background_summary",
    "builtin_turn_agent",
    "router",
]
