"""Публичный API тенанта для виджета/чата (docs/contracts.md §1)."""

import structlog
from fastapi import APIRouter, status

from app.contracts import PublicConfig
from app.modules.shared.public import ApiError, DbSession
from app.modules.tenants.api.deps import WidgetTenant
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
