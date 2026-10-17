"""Публичный интерфейс модуля leads — единственная точка входа для других модулей.

`SqlLeadStore` и `LeadRepository` — конкретные классы: их подключают сборщики приложения
(main.py, retention), остальной код работает через порт tools `LeadStore` (REVIEW.md: L-13)."""

from app.modules.leads.api.admin_router import router as admin_router
from app.modules.leads.domain.lead import Lead
from app.modules.leads.infrastructure.models import LeadRecord
from app.modules.leads.infrastructure.repositories import LeadRepository, SqlLeadStore

__all__ = ["Lead", "LeadRecord", "LeadRepository", "SqlLeadStore", "admin_router"]
