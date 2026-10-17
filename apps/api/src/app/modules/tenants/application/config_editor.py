"""Редактор AgentConfig в админке: YAML ↔ JSON, валидация, черновик → активная версия.

Версии неизменяемы: сохранение создаёт новый черновик, откат — активация архивной версии.
Проверка та же, что у seed тенанта (`TenantSpec`): схема AgentConfig и уникальные ключи
сценариев.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import structlog
import yaml
from pydantic import ValidationError

from app.contracts import AgentConfig
from app.modules.shared.kernel import TenantId
from app.modules.tenants.domain.entities import AgentConfigOrigin, AgentConfigVersion
from app.modules.tenants.domain.errors import (
    AgentConfigNotFoundError,
    ConfigProblem,
    InvalidAgentConfigError,
)
from app.modules.tenants.domain.ports import AgentConfigStore

# Журнал действий админки (REVIEW.md: T-4): admin_user_id — в контексте лога от AdminEditor.
logger = structlog.get_logger(__name__)


def scenario_key_problems(config: AgentConfig) -> list[ConfigProblem]:
    """Ключ сценария — значение `active_scenario` в состоянии диалога: дубль неоднозначен."""
    keys = [s.key for s in config.prompt.scenarios or []]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if not duplicates:
        return []
    return [
        ConfigProblem(
            ("prompt", "scenarios"), f"повторяются ключи сценариев: {', '.join(duplicates)}"
        )
    ]


def validate_agent_config(data: Any) -> dict[str, Any]:
    """AgentConfig в виде для БД: как написан, без подставленных значений по умолчанию
    (они применяются при чтении)."""
    try:
        config = AgentConfig.model_validate(data)
    except ValidationError as e:
        raise InvalidAgentConfigError(
            [ConfigProblem(tuple(err["loc"]), err["msg"]) for err in e.errors()]
        ) from e
    problems = scenario_key_problems(config)
    if problems:
        raise InvalidAgentConfigError(problems)
    return config.model_dump(mode="json", exclude_unset=True)


def parse_agent_config_yaml(source: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"строка {mark.line + 1}, столбец {mark.column + 1}: " if mark else ""
        problem = getattr(e, "problem", None) or str(e)
        raise InvalidAgentConfigError([ConfigProblem((), f"YAML: {where}{problem}")]) from e
    if not isinstance(data, dict):
        raise InvalidAgentConfigError([ConfigProblem((), "ожидается объект AgentConfig")])
    return validate_agent_config(data)


def agent_config_yaml(config: dict[str, Any]) -> str:
    """YAML для редактора. Порядок ключей — как у полей AgentConfig: JSONB его не хранит.
    Версия, устаревшая по схеме, отдаётся как есть: её открывают, чтобы исправить."""
    try:
        ordered = AgentConfig.model_validate(config).model_dump(mode="json", exclude_unset=True)
    except ValidationError:
        ordered = config
    return yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=100)


async def list_config_versions(
    tenant_id: TenantId, configs: AgentConfigStore
) -> list[AgentConfigVersion]:
    """От новой версии к старой."""
    versions: Sequence[AgentConfigVersion] = await configs.list_versions(tenant_id)
    return sorted(versions, key=lambda v: v.version, reverse=True)


async def get_config_version(
    tenant_id: TenantId, config_id: UUID, configs: AgentConfigStore
) -> AgentConfigVersion:
    version = await configs.get_version(tenant_id, config_id)
    if version is None:
        raise AgentConfigNotFoundError(f"версия конфига {config_id} не найдена")
    return version


async def save_config_draft(
    tenant_id: TenantId, source: str, configs: AgentConfigStore
) -> AgentConfigVersion:
    draft = await configs.create_draft(
        tenant_id, parse_agent_config_yaml(source), AgentConfigOrigin.ADMIN
    )
    logger.info(
        "agent_config_draft_saved",
        tenant_id=str(tenant_id),
        config_id=str(draft.id),
        version=draft.version,
    )
    return draft


async def activate_config_version(
    tenant_id: TenantId, config_id: UUID, configs: AgentConfigStore
) -> AgentConfigVersion:
    """Черновик или архивная версия (откат) становится активной, прежняя — архивной.
    Конфиг проверяется заново: схема могла измениться после сохранения версии."""
    version = await get_config_version(tenant_id, config_id, configs)
    validate_agent_config(version.config)
    activation = await configs.activate(tenant_id, config_id)
    active = activation.config
    logger.info(
        "agent_config_activated",
        tenant_id=str(tenant_id),
        config_id=str(active.id),
        version=active.version,
        previous_version=activation.previous_version,
        changed=activation.previous_version != active.version,
    )
    return active
