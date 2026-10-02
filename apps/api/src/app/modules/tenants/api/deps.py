"""FastAPI-зависимости публичного API: тенант по ключу виджета."""

from typing import Annotated

import structlog
from fastapi import Depends, Header, HTTPException, status

from app.modules.shared.public import DbSession, TenantId
from app.modules.tenants.application.public_config import authenticate_widget
from app.modules.tenants.domain.errors import InvalidWidgetKeyError
from app.modules.tenants.infrastructure.repositories import SqlWidgetKeyResolver


async def require_widget_tenant(
    session: DbSession,
    x_widget_key: Annotated[str | None, Header()] = None,
) -> TenantId:
    """401, если ключ не передан или неверен. tenant_id попадает во все логи запроса."""
    try:
        access = await authenticate_widget(x_widget_key, SqlWidgetKeyResolver(session))
    except InvalidWidgetKeyError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e
    structlog.contextvars.bind_contextvars(tenant_id=str(access.tenant_id))
    return access.tenant_id


WidgetTenant = Annotated[TenantId, Depends(require_widget_tenant)]
