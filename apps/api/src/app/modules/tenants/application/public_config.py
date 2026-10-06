"""Публичный доступ виджета: проверка ключа и публичная часть конфига."""

from collections.abc import Collection

from app.contracts import AgentConfig, PublicAssistant, PublicConfig
from app.contracts.generated.agent_config_schema import BrandingConfig
from app.modules.shared.kernel import TenantId
from app.modules.tenants.domain.entities import WidgetAccess
from app.modules.tenants.domain.errors import (
    InvalidWidgetKeyError,
    NoActiveConfigError,
    OriginNotAllowedError,
)
from app.modules.tenants.domain.language import assistant_texts, resolve_language
from app.modules.tenants.domain.ports import AgentConfigStore, WidgetKeyResolver
from app.modules.tenants.domain.widget_keys import hash_widget_key, origin_allowed


async def authenticate_widget(
    key: str | None,
    resolver: WidgetKeyResolver,
    origin: str | None = None,
    platform_origins: Collection[str] = (),
) -> WidgetAccess:
    """Тенант по ключу; `origin` — заголовок `Origin` запроса, `platform_origins` — свой
    веб-чат (ADR-0022)."""
    if not key:
        raise InvalidWidgetKeyError("X-Widget-Key не передан")
    access = await resolver.resolve(hash_widget_key(key))
    if access is None:
        raise InvalidWidgetKeyError("неизвестный ключ виджета")
    if not origin_allowed(origin, access.allowed_origins, platform_origins):
        raise OriginNotAllowedError("ключ виджета не разрешён для этого сайта")
    return access


async def get_public_config(
    tenant_id: TenantId, configs: AgentConfigStore, locale: str | None = None
) -> PublicConfig:
    """Публичная часть конфига на языке, который получит диалог с этим `locale` (ADR-0025)."""
    active = await configs.get_active(tenant_id)
    if active is None:
        raise NoActiveConfigError(f"у тенанта {tenant_id} нет активного AgentConfig")
    config = AgentConfig.model_validate(active.config)
    assistant = config.assistant
    language = resolve_language(assistant, locale)
    texts = assistant_texts(assistant, language)
    return PublicConfig(
        assistant=PublicAssistant(
            name=assistant.name,
            language=language,
            greeting=texts.greeting,
            starter_suggestions=list(texts.starter_suggestions),
        ),
        branding=config.branding or BrandingConfig(),
    )
