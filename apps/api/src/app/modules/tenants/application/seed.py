"""Seed тенанта из описания: идемпотентно создаёт/обновляет тенанта, конфиг и ключ виджета."""

from dataclasses import dataclass, replace

from app.modules.shared.kernel import TenantId
from app.modules.tenants.application.spec import TenantSpec
from app.modules.tenants.domain.entities import AgentConfigOrigin, AgentConfigVersion, Tenant
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
    # YAML изменился с прошлого seed, но не применён: активную версию выбрали в админке (R-1).
    config_kept: bool = False


async def seed_tenant(
    spec: TenantSpec,
    *,
    tenants: TenantDirectory,
    configs: AgentConfigStore,
    widget_keys: WidgetKeyStore,
    widget_key: str | None = None,
    reset_widget_key: bool = False,
    force_config: bool = False,
) -> SeedResult:
    """Повторный запуск с тем же описанием ничего не меняет.

    Конфиг, отличающийся от активного, становится новой активной версией, только если
    активна последняя версия из seed: правку или откат в админке (в т.ч. сроки хранения,
    ADR-0043) seed не затирает (REVIEW.md: R-1). Активация этой версии в админке
    возвращает управление YAML. Изменённый с прошлого seed YAML при выборе админки —
    `config_kept`; неизменённый — без сообщения. `force_config` применяет YAML всё равно.
    Ключ виджета
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
    # Блокировка до чтения: активация в админке между проверкой и записью была бы затёрта.
    await configs.lock(tenant.id)
    active = await configs.get_active(tenant.id)
    changed = kept = False
    if active is None or active.config != config:
        seeded = await _latest_seed_version(configs, tenant.id)
        if active is None or force_config or (seeded is not None and seeded.id == active.id):
            draft = await configs.create_draft(tenant.id, config, AgentConfigOrigin.SEED)
            active = (await configs.activate(tenant.id, draft.id)).config
            changed = True
        else:
            kept = seeded is None or seeded.config != config

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
        config_kept=kept,
    )


async def _latest_seed_version(
    configs: AgentConfigStore, tenant_id: TenantId
) -> AgentConfigVersion | None:
    versions = await configs.list_versions(tenant_id)
    seeded = [v for v in versions if v.origin == AgentConfigOrigin.SEED]
    return seeded[-1] if seeded else None
