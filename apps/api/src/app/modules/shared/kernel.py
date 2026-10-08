"""Общее ядро shared без фреймворков: типы и ошибки, доступные любому слою, включая domain.

`public.py` тянет SQLAlchemy и FastAPI, поэтому domain-слои модулей импортируют общие
типы отсюда (ADR-0007).
"""

from app.modules.shared.domain.admin import AdminAuthenticator, AdminPrincipal, AdminRole
from app.modules.shared.domain.errors import DomainError, NotFoundError, TenantMismatchError
from app.modules.shared.domain.ids import TenantId
from app.modules.shared.domain.rate_limit import RateDecision, RateLimit, RateLimiter

__all__ = [
    "AdminAuthenticator",
    "AdminPrincipal",
    "AdminRole",
    "DomainError",
    "NotFoundError",
    "RateDecision",
    "RateLimit",
    "RateLimiter",
    "TenantId",
    "TenantMismatchError",
]
