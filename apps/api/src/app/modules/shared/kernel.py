"""Общее ядро shared без фреймворков: типы и ошибки, доступные любому слою, включая domain.

`public.py` тянет SQLAlchemy и FastAPI, поэтому domain-слои модулей импортируют общие
типы отсюда (ADR-0007).
"""

from app.modules.shared.domain.errors import DomainError, NotFoundError, TenantMismatchError
from app.modules.shared.domain.ids import TenantId

__all__ = ["DomainError", "NotFoundError", "TenantId", "TenantMismatchError"]
