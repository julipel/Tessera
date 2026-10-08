"""Реестр ORM-моделей для Alembic autogenerate.

Каждый модуль с таблицами добавляет сюда импорт своих моделей (через public.py),
чтобы они попали в `Base.metadata`.
"""

from app.modules.access.public import AdminMembershipRecord, AdminUserRecord
from app.modules.chat.public import ConversationRecord, MessageRecord, ToolCallRecord
from app.modules.knowledge.public import (
    ChunkRecord,
    DocumentRecord,
    EntityRecord,
    SourceRecord,
    SourceSyncRecord,
)
from app.modules.leads.public import LeadRecord
from app.modules.observability.public import AgentEventRecord
from app.modules.shared.public import Base
from app.modules.tenants.public import AgentConfigRecord, TenantRecord, WidgetKeyRecord

metadata = Base.metadata
_registered = (
    TenantRecord,
    AgentConfigRecord,
    WidgetKeyRecord,
    ConversationRecord,
    MessageRecord,
    ToolCallRecord,
    SourceRecord,
    SourceSyncRecord,
    EntityRecord,
    DocumentRecord,
    ChunkRecord,
    LeadRecord,
    AgentEventRecord,
    AdminUserRecord,
    AdminMembershipRecord,
)

__all__ = ["metadata"]
