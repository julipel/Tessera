"""Публичный API тенанта для виджета/чата (docs/contracts.md §1)."""

from fastapi import APIRouter, HTTPException, status

from app.contracts import PublicConfig
from app.modules.shared.public import DbSession
from app.modules.tenants.api.deps import WidgetTenant
from app.modules.tenants.application.public_config import get_public_config
from app.modules.tenants.domain.errors import NoActiveConfigError
from app.modules.tenants.infrastructure.repositories import AgentConfigRepository

router = APIRouter(prefix="/v1/public", tags=["public"])


@router.get("/config")
async def public_config(tenant_id: WidgetTenant, session: DbSession) -> PublicConfig:
    try:
        return await get_public_config(tenant_id, AgentConfigRepository(session))
    except NoActiveConfigError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e
