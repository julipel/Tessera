"""Публичный доступ виджета: проверка ключа и публичная часть конфига."""

from app.contracts import AgentConfig, PublicAssistant, PublicConfig
from app.contracts.generated.agent_config_schema import BrandingConfig
from app.modules.shared.kernel import TenantId
from app.modules.tenants.domain.entities import WidgetAccess
from app.modules.tenants.domain.errors import InvalidWidgetKeyError, NoActiveConfigError
from app.modules.tenants.domain.ports import AgentConfigStore, WidgetKeyResolver
from app.modules.tenants.domain.widget_keys import hash_widget_key


async def authenticate_widget(key: str | None, resolver: WidgetKeyResolver) -> WidgetAccess:
    if not key:
        raise InvalidWidgetKeyError("X-Widget-Key не передан")
    access = await resolver.resolve(hash_widget_key(key))
    if access is None:
        raise InvalidWidgetKeyError("неизвестный ключ виджета")
    return access


async def get_public_config(tenant_id: TenantId, configs: AgentConfigStore) -> PublicConfig:
    active = await configs.get_active(tenant_id)
    if active is None:
        raise NoActiveConfigError(f"у тенанта {tenant_id} нет активного AgentConfig")
    config = AgentConfig.model_validate(active.config)
    assistant = config.assistant
    return PublicConfig(
        assistant=PublicAssistant(
            name=assistant.name,
            language=assistant.language or "auto",
            greeting=assistant.greeting,
            starter_suggestions=assistant.starter_suggestions or [],
        ),
        branding=config.branding or BrandingConfig(),
    )
