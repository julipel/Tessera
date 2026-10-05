"""FastAPI-зависимости публичного API: тенант по ключу виджета."""

from typing import Annotated

import structlog
from fastapi import Depends, Header, Request, status

from app.modules.shared.public import ApiError, DbSession, TenantId
from app.modules.tenants.application.public_config import authenticate_widget
from app.modules.tenants.domain.entities import WidgetAccess
from app.modules.tenants.domain.errors import InvalidWidgetKeyError, OriginNotAllowedError
from app.modules.tenants.infrastructure.repositories import SqlWidgetKeyResolver


async def require_widget_access(
    request: Request,
    session: DbSession,
    x_widget_key: Annotated[str | None, Header()] = None,
    origin: Annotated[str | None, Header()] = None,
) -> WidgetAccess:
    """401, если ключ не передан или неверен; 403 — запрос со страницы чужого сайта
    (ADR-0022). tenant_id попадает во все логи запроса."""
    # Свой веб-чат — те же origin, что разрешены CORS.
    platform_origins: list[str] = request.app.state.settings.cors_origins
    try:
        access = await authenticate_widget(
            x_widget_key, SqlWidgetKeyResolver(session), origin, platform_origins
        )
    except InvalidWidgetKeyError as e:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "unauthorized", str(e)) from e
    except OriginNotAllowedError as e:
        raise ApiError(status.HTTP_403_FORBIDDEN, "forbidden", str(e)) from e
    structlog.contextvars.bind_contextvars(tenant_id=str(access.tenant_id))
    return access


async def require_widget_tenant(
    access: Annotated[WidgetAccess, Depends(require_widget_access)],
) -> TenantId:
    return access.tenant_id


WidgetTenant = Annotated[TenantId, Depends(require_widget_tenant)]
WidgetAccessDep = Annotated[WidgetAccess, Depends(require_widget_access)]
