"""Seed тенанта из описания: идемпотентно создаёт/обновляет тенанта, конфиг и ключ виджета."""

from dataclasses import dataclass, replace

from app.modules.tenants.application.spec import TenantSpec
from app.modules.tenants.domain.entities import AgentConfigVersion, Tenant
from app.modules.tenants.domain.ports import AgentConfigStore, TenantDirectory, WidgetKeyStore
from app.modules.tenants.domain.widget_keys import generate_widget_key, hash_widget_key


@dataclass(frozen=True, slots=True)
class SeedResult:
    tenant: Tenant
    active_config: AgentConfigVersion
    config_changed: bool
    # Открытый ключ виджета — только если он создан в этом запуске (в БД лежит хэш).
    new_widget_key: str | None
    # Переданный `widget_key` не применён: у тенанта уже есть другой ключ (REVIEW.md: T-7).
    widget_key_ignored: bool = False


async def seed_tenant(
    spec: TenantSpec,
    *,
    tenants: TenantDirectory,
    configs: AgentConfigStore,
    widget_keys: WidgetKeyStore,
    widget_key: str | None = None,
    reset_widget_key: bool = False,
) -> SeedResult:
    """Повторный запуск с тем же описанием ничего не меняет.

    Конфиг, отличающийся от активного, становится новой активной версией. Ключ виджета
    создаётся, только если у тенанта ещё нет ни одного (`widget_key` — заданный извне,
    иначе генерируется). Заданный ключ при другом уже выпущенном не применяется —
    `widget_key_ignored`. `reset_widget_key` удаляет все ключи тенанта и выпускает новый.
    """
    tenant = await tenants.get_by_slug(spec.tenant.slug)
    if tenant is None:
        tenant = await tenants.create(spec.tenant.slug, spec.tenant.name)
    elif tenant.name != spec.tenant.name:
        await tenants.rename(tenant.id, spec.tenant.name)
        tenant = replace(tenant, name=spec.tenant.name)

    config = spec.config_json()
    active = await configs.get_active(tenant.id)
    if active is None or active.config != config:
        draft = await configs.create_draft(tenant.id, config)
        active = (await configs.activate(tenant.id, draft.id)).config
        changed = True
    else:
        changed = False

    if reset_widget_key:
        await widget_keys.delete_all(tenant.id)
    new_key: str | None = None
    ignored = False
    if not await widget_keys.has_any(tenant.id):
        new_key = widget_key or generate_widget_key()
        await widget_keys.add_key(tenant.id, hash_widget_key(new_key), spec.widget.allowed_origins)
    elif widget_key is not None:
        ignored = not await widget_keys.has_key(tenant.id, hash_widget_key(widget_key))

    return SeedResult(
        tenant=tenant,
        active_config=active,
        config_changed=changed,
        new_widget_key=new_key,
        widget_key_ignored=ignored,
    )
