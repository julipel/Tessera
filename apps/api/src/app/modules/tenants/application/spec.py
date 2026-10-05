"""Описание тенанта в YAML (config/tenants/*.yaml): метаданные, виджет, AgentConfig, источники."""

from typing import Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from app.contracts import AgentConfig
from app.modules.tenants.domain.errors import InvalidTenantSpecError
from app.modules.tenants.domain.widget_keys import ORIGIN_PATTERN

Origin = Annotated[str, StringConstraints(pattern=ORIGIN_PATTERN)]


class TenantInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=64)
    name: str = Field(min_length=1, max_length=200)


class WidgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Сайты, где можно встроить виджет (ADR-0022): `https://shop.example`, без пути.
    allowed_origins: list[Origin] = Field(default_factory=list)


class TenantSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: TenantInfo
    widget: WidgetSpec = Field(default_factory=WidgetSpec)
    agent_config: AgentConfig
    # Декларации источников (ADR-0019) разбирает knowledge, здесь — как написаны.
    sources: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("agent_config")
    @classmethod
    def _unique_scenario_keys(cls, config: AgentConfig) -> AgentConfig:
        # Ключ сценария — значение `active_scenario` в состоянии диалога: дубль неоднозначен.
        keys = [s.key for s in config.prompt.scenarios or []]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(f"повторяются ключи сценариев: {', '.join(duplicates)}")
        return config

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
