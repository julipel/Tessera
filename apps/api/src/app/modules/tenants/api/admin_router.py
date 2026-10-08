"""API админки: версии AgentConfig (docs/contracts.md §7); доступ — роль editor в тенанте."""

from uuid import UUID

from fastapi import APIRouter, status

from app.contracts import (
    AgentConfigVersionDetail,
    AgentConfigVersionList,
    AgentConfigVersionSummary,
    CreateAgentConfigRequest,
    ErrorDetail,
)
from app.modules.shared.public import AdminEditor, ApiError, DbSession, TenantId
from app.modules.tenants.application.config_editor import (
    activate_config_version,
    agent_config_yaml,
    get_config_version,
    list_config_versions,
    save_config_draft,
)
from app.modules.tenants.domain.entities import AgentConfigVersion
from app.modules.tenants.domain.errors import AgentConfigNotFoundError, InvalidAgentConfigError
from app.modules.tenants.infrastructure.repositories import AgentConfigRepository

router = APIRouter(
    prefix="/v1/admin/tenants/{tenant_id}/agent-configs", tags=["admin"], dependencies=[AdminEditor]
)


@router.get("")
async def agent_config_versions(tenant_id: UUID, session: DbSession) -> AgentConfigVersionList:
    versions = await list_config_versions(TenantId(tenant_id), AgentConfigRepository(session))
    return AgentConfigVersionList(
        versions=[
            AgentConfigVersionSummary(
                id=v.id, version=v.version, status=v.status, created_at=v.created_at
            )
            for v in versions
        ]
    )


@router.get("/{config_id}")
async def agent_config_version(
    tenant_id: UUID, config_id: UUID, session: DbSession
) -> AgentConfigVersionDetail:
    try:
        version = await get_config_version(
            TenantId(tenant_id), config_id, AgentConfigRepository(session)
        )
    except AgentConfigNotFoundError as e:
        raise _not_found() from e
    return _detail(version)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent_config_draft(
    tenant_id: UUID, body: CreateAgentConfigRequest, session: DbSession
) -> AgentConfigVersionDetail:
    """Новый черновик из YAML; 422 `invalid_input` с `details` — ошибки по местам конфига."""
    try:
        version = await save_config_draft(
            TenantId(tenant_id), body.yaml, AgentConfigRepository(session)
        )
    except InvalidAgentConfigError as e:
        raise _invalid(e) from e
    return _detail(version)


@router.post("/{config_id}/activate")
async def activate_agent_config(
    tenant_id: UUID, config_id: UUID, session: DbSession
) -> AgentConfigVersionDetail:
    """Черновик или архивная версия (откат) становится активной; новые диалоги начинаются
    на ней, начатые остаются на своей версии."""
    try:
        version = await activate_config_version(
            TenantId(tenant_id), config_id, AgentConfigRepository(session)
        )
    except AgentConfigNotFoundError as e:
        raise _not_found() from e
    except InvalidAgentConfigError as e:
        raise _invalid(e) from e
    return _detail(version)


def _detail(version: AgentConfigVersion) -> AgentConfigVersionDetail:
    return AgentConfigVersionDetail(
        id=version.id,
        version=version.version,
        status=version.status,
        created_at=version.created_at,
        yaml=agent_config_yaml(version.config),
    )


def _not_found() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "not_found", "версия конфига не найдена")


def _invalid(error: InvalidAgentConfigError) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "invalid_input",
        str(error),
        details=[ErrorDetail(loc=list(p.loc), message=p.message) for p in error.problems],
    )
