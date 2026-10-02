"""Реестр ORM-моделей для Alembic autogenerate.

Каждый модуль с таблицами добавляет сюда импорт своих моделей (через public.py),
чтобы они попали в `Base.metadata`.
"""

from app.modules.chat.public import ConversationRecord, MessageRecord
from app.modules.shared.public import Base
from app.modules.tenants.public import AgentConfigRecord, TenantRecord, WidgetKeyRecord

metadata = Base.metadata
_registered = (TenantRecord, AgentConfigRecord, WidgetKeyRecord, ConversationRecord, MessageRecord)

__all__ = ["metadata"]
