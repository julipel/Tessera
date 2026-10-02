"""Описание тенанта в YAML (config/tenants/*.yaml): метаданные, виджет, AgentConfig."""

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.contracts import AgentConfig
from app.modules.tenants.domain.errors import InvalidTenantSpecError


class TenantInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=64)
    name: str = Field(min_length=1, max_length=200)


class WidgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_origins: list[str] = Field(default_factory=list)


class TenantSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: TenantInfo
    widget: WidgetSpec = Field(default_factory=WidgetSpec)
    agent_config: AgentConfig

    def config_json(self) -> dict[str, Any]:
        # exclude_unset: в БД — конфиг в том виде, в каком он написан, без подставленных
        # по умолчанию значений (они применяются при чтении).
        return self.agent_config.model_dump(mode="json", exclude_unset=True)


def load_tenant_spec(source: str) -> TenantSpec:
    try:
        data = yaml.safe_load(source)
        return TenantSpec.model_validate(data)
    except (yaml.YAMLError, ValidationError) as e:
        raise InvalidTenantSpecError(str(e)) from e
