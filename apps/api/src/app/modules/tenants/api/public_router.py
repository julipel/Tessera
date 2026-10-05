"""Публичный API тенанта для виджета/чата (docs/contracts.md §1)."""

import structlog
from fastapi import APIRouter, status

from app.contracts import PublicConfig, WidgetEmbed
from app.modules.shared.public import ApiError, DbSession
from app.modules.tenants.api.deps import WidgetAccessDep, WidgetTenant
from app.modules.tenants.application.public_config import get_public_config
from app.modules.tenants.domain.errors import NoActiveConfigError
from app.modules.tenants.infrastructure.repositories import AgentConfigRepository

router = APIRouter(prefix="/v1/public", tags=["public"])
logger = structlog.get_logger(__name__)


@router.get("/config")
async def public_config(tenant_id: WidgetTenant, session: DbSession) -> PublicConfig:
    try:
        config = await get_public_config(tenant_id, AgentConfigRepository(session))
    except NoActiveConfigError as e:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", str(e)) from e
    logger.info("public_config_served")
    return config


@router.get("/widget")
async def widget_embed(access: WidgetAccessDep) -> WidgetEmbed:
    """Где можно встроить виджет: сервер веб-чата ставит по ответу CSP frame-ancestors
    на странице iframe (ADR-0022)."""
    return WidgetEmbed(allowed_origins=list(access.allowed_origins))
