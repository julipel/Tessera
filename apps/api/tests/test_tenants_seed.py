"""Seed тенанта из YAML: валидация описания и идемпотентность."""

from pathlib import Path

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tenants.application.config_editor import save_config_draft
from app.modules.tenants.public import (
    AgentConfigOrigin,
    AgentConfigRepository,
    AgentConfigStatus,
    InvalidTenantSpecError,
    SeedResult,
    SqlTenantDirectory,
    TenantSpec,
    WidgetKeyRepository,
    hash_widget_key,
    load_tenant_spec,
    seed_tenant,
)

TENANTS_DIR = Path(__file__).resolve().parents[3] / "config" / "tenants"

SPEC_YAML = """
tenant: {slug: shop, name: "Shop"}
widget: {allowed_origins: ["https://shop.example"]}
agent_config:
  assistant: {name: "Ассистент", greeting: "Привет!", fallback_message: "Ошибка"}
  model: {primary: {provider: openai, name: "m"}}
  limits: {}
  prompt: {tenant: "Ты — консультант."}
  tools: {builtin: [search_catalog]}
"""


async def _seed(
    session: AsyncSession,
    spec: TenantSpec,
    widget_key: str | None = None,
    *,
    force_config: bool = False,
) -> SeedResult:
    return await seed_tenant(
        spec,
        tenants=SqlTenantDirectory(session),
        configs=AgentConfigRepository(session),
        widget_keys=WidgetKeyRepository(session),
        widget_key=widget_key,
        force_config=force_config,
    )


def _spec(**assistant: str) -> TenantSpec:
    spec = load_tenant_spec(SPEC_YAML)
    return spec.model_copy(
        update={
            "agent_config": spec.agent_config.model_copy(
                update={"assistant": spec.agent_config.assistant.model_copy(update=assistant)}
            )
        }
    )


@pytest.mark.parametrize("path", sorted(TENANTS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_tenant_files_are_valid(path: Path) -> None:
    spec = load_tenant_spec(path.read_text(encoding="utf-8"))
    assert spec.tenant.slug == path.stem


def test_demo_tenant_exists() -> None:
    assert (TENANTS_DIR / "demo-beauty.yaml").exists()


@pytest.mark.parametrize(
    "source",
    [
        "tenant: [unclosed",
        SPEC_YAML + "extra: 1\n",
        SPEC_YAML.replace("provider: openai", "provider: unknown"),
        SPEC_YAML.replace("slug: shop", "slug: Shop!"),
    ],
    ids=["broken-yaml", "unknown-field", "invalid-agent-config", "invalid-slug"],
)
def test_invalid_spec_is_rejected(source: str) -> None:
    with pytest.raises(InvalidTenantSpecError):
        load_tenant_spec(source)


@pytest.mark.parametrize(
    "branding",
    [
        '{tokens: {primary: "red; background: url(x)"}}',
        '{tokens: {primary: "#12345"}}',
        '{tokens: {radius: "12"}}',
        '{tokens: {font: "Inter; color: red"}}',
        '{logo_url: "javascript:alert(1)"}',
        '{logo_url: "/logo.svg) url(x"}',
    ],
)
def test_invalid_branding_is_rejected(branding: str) -> None:
    # Токены попадают в CSS-переменные чата — форматы проверяются уже при загрузке YAML.
    with pytest.raises(InvalidTenantSpecError):
        load_tenant_spec(SPEC_YAML + f"  branding: {branding}\n")


def test_valid_branding_is_accepted() -> None:
    tokens = '{primary: "#2E6B3F", radius: "0.5rem", font: "PT Serif"}'
    branding = f'{{tokens: {tokens}, logo_url: "/l.svg"}}'
    spec = load_tenant_spec(SPEC_YAML + f"  branding: {branding}\n")
    assert spec.agent_config.branding is not None
    assert spec.agent_config.branding.logo_url == "/l.svg"


@pytest.mark.parametrize(
    "origin",
    [
        "https://shop.example/",
        "https://shop.example/path",
        "https://*.shop.example",
        "*",
        "https://shop.example; script-src *",
        "HTTPS://Shop.example",
        "shop.example",
        "ftp://shop.example",
    ],
)
def test_invalid_allowed_origin_is_rejected(origin: str) -> None:
    # Origin попадает в CSP frame-ancestors страницы виджета (ADR-0022).
    source = SPEC_YAML.replace('"https://shop.example"', f'"{origin}"')
    with pytest.raises(InvalidTenantSpecError):
        load_tenant_spec(source)


def test_valid_allowed_origins_are_accepted() -> None:
    origins = '["https://shop.example", "http://localhost:3000", "http://127.0.0.2:3001"]'
    spec = load_tenant_spec(SPEC_YAML.replace('["https://shop.example"]', origins))
    assert spec.widget.allowed_origins[1] == "http://localhost:3000"


def test_duplicate_scenario_keys_are_rejected() -> None:
    scenario = '{key: gift, description: "Подарок", instructions: "Выясни бюджет."}'
    prompt = f'prompt: {{tenant: "Ты — консультант.", scenarios: [{scenario}, {scenario}]}}'
    source = SPEC_YAML.replace('prompt: {tenant: "Ты — консультант."}', prompt)

    with pytest.raises(InvalidTenantSpecError, match="gift"):
        load_tenant_spec(source)


def test_config_is_stored_as_written() -> None:
    config = load_tenant_spec(SPEC_YAML).config_json()
    # Значения по умолчанию из схемы не материализуются.
    assert config["limits"] == {}
    assert "language" not in config["assistant"]


async def test_first_seed_creates_tenant_config_and_key(db_session: AsyncSession) -> None:
    result = await _seed(db_session, load_tenant_spec(SPEC_YAML))

    assert result.tenant.slug == "shop"
    assert result.config_changed is True
    assert result.active_config.version == 1
    assert result.active_config.status == AgentConfigStatus.ACTIVE
    assert result.new_widget_key is not None
    assert result.new_widget_key.startswith("wk_")

    [key] = await WidgetKeyRepository(db_session).list(result.tenant.id)
    assert key.key_hash == hash_widget_key(result.new_widget_key)
    assert key.allowed_origins == ["https://shop.example"]


async def test_seed_uses_given_widget_key(db_session: AsyncSession) -> None:
    result = await _seed(db_session, load_tenant_spec(SPEC_YAML), widget_key="wk_local")

    assert result.new_widget_key == "wk_local"
    [key] = await WidgetKeyRepository(db_session).list(result.tenant.id)
    assert key.key_hash == hash_widget_key("wk_local")


async def test_repeated_seed_changes_nothing(db_session: AsyncSession) -> None:
    first = await _seed(db_session, load_tenant_spec(SPEC_YAML))
    second = await _seed(db_session, load_tenant_spec(SPEC_YAML))

    assert second.tenant.id == first.tenant.id
    assert second.config_changed is False
    assert second.active_config.id == first.active_config.id
    assert second.new_widget_key is None
    assert len(await AgentConfigRepository(db_session).list_versions(first.tenant.id)) == 1
    assert len(await WidgetKeyRepository(db_session).list(first.tenant.id)) == 1


async def test_changed_config_becomes_new_active_version(db_session: AsyncSession) -> None:
    first = await _seed(db_session, _spec())
    second = await _seed(db_session, _spec(greeting="Здравствуйте!"))

    assert second.config_changed is True
    assert second.active_config.version == 2
    assert second.active_config.config["assistant"]["greeting"] == "Здравствуйте!"
    versions = await AgentConfigRepository(db_session).list_versions(first.tenant.id)
    assert [v.status for v in versions] == [AgentConfigStatus.ARCHIVED, AgentConfigStatus.ACTIVE]


# --- конфиг из админки seed не затирает (REVIEW.md: R-1) ---


async def _edit_in_admin(session: AsyncSession, seeded: SeedResult) -> int:
    """Правка в админке: новая версия из редактора (сроки хранения) и её активация."""
    configs = AgentConfigRepository(session)
    edited = {**seeded.active_config.config, "retention": {"conversations_days": 365}}
    draft = await configs.create_draft(seeded.tenant.id, edited, AgentConfigOrigin.ADMIN)
    return (await configs.activate(seeded.tenant.id, draft.id)).config.version


async def test_seed_versions_are_marked_seed(db_session: AsyncSession) -> None:
    result = await _seed(db_session, _spec())
    assert result.active_config.origin == AgentConfigOrigin.SEED


async def test_seed_keeps_config_activated_in_admin(db_session: AsyncSession) -> None:
    first = await _seed(db_session, _spec())
    admin_version = await _edit_in_admin(db_session, first)

    # Тот же YAML — без сообщения, изменённый — config_kept; версия админки остаётся.
    for spec, kept in ((_spec(), False), (_spec(greeting="Здравствуйте!"), True)):
        again = await _seed(db_session, spec)
        assert again.config_kept is kept
        assert again.config_changed is False
        assert again.active_config.version == admin_version
        assert again.active_config.config["retention"] == {"conversations_days": 365}
    versions = await AgentConfigRepository(db_session).list_versions(first.tenant.id)
    assert len(versions) == 2


async def test_seed_keeps_rollback_to_older_seed_version(db_session: AsyncSession) -> None:
    """Откат в админке на прежнюю версию из seed — тоже выбор админки."""
    first = await _seed(db_session, _spec())
    await _seed(db_session, _spec(greeting="v2"))
    configs = AgentConfigRepository(db_session)
    await configs.activate(first.tenant.id, first.active_config.id)

    again = await _seed(db_session, _spec(greeting="v3"))

    assert again.config_kept is True
    assert again.active_config.id == first.active_config.id


async def test_activating_latest_seed_version_returns_control_to_yaml(
    db_session: AsyncSession,
) -> None:
    """Откат в админке на последнюю версию из seed — «вернуть YAML» без CLI."""
    first = await _seed(db_session, _spec())
    await _edit_in_admin(db_session, first)
    await AgentConfigRepository(db_session).activate(first.tenant.id, first.active_config.id)

    again = await _seed(db_session, _spec(greeting="Здравствуйте!"))

    assert again.config_changed is True
    assert again.active_config.version == 3


async def test_force_config_applies_yaml_over_admin(db_session: AsyncSession) -> None:
    first = await _seed(db_session, _spec())
    await _edit_in_admin(db_session, first)

    forced = await _seed(db_session, _spec(greeting="Здравствуйте!"), force_config=True)

    assert forced.config_changed is True and forced.config_kept is False
    assert forced.active_config.version == 3
    assert forced.active_config.origin == AgentConfigOrigin.SEED
    assert "retention" not in forced.active_config.config
    # После --force-config seed снова ведёт конфиг: следующая правка YAML применяется.
    later = await _seed(db_session, _spec(greeting="v4"))
    assert later.config_changed is True and later.active_config.version == 4


async def test_admin_editor_marks_versions_admin(db_session: AsyncSession) -> None:
    first = await _seed(db_session, _spec())
    source = yaml.safe_dump(first.active_config.config, allow_unicode=True)

    draft = await save_config_draft(first.tenant.id, source, AgentConfigRepository(db_session))

    assert draft.origin == AgentConfigOrigin.ADMIN


async def test_admin_draft_does_not_block_seed(db_session: AsyncSession) -> None:
    """Неактивированный черновик в админке — не выбор: активна последняя версия из seed."""
    first = await _seed(db_session, _spec())
    await AgentConfigRepository(db_session).create_draft(
        first.tenant.id, first.active_config.config, AgentConfigOrigin.ADMIN
    )

    again = await _seed(db_session, _spec(greeting="Здравствуйте!"))

    assert again.config_changed is True
    assert again.active_config.version == 3


async def test_seed_renames_tenant(db_session: AsyncSession) -> None:
    await _seed(db_session, load_tenant_spec(SPEC_YAML))
    renamed = await _seed(
        db_session, load_tenant_spec(SPEC_YAML.replace('name: "Shop"', 'name: "Shop 2"'))
    )

    tenant = await SqlTenantDirectory(db_session).get_by_slug("shop")
    assert tenant is not None
    assert tenant.name == "Shop 2"
    # Результат — с новым именем, а не прочитанным до переименования (REVIEW.md: T-6).
    assert renamed.tenant == tenant


async def test_given_key_is_reported_when_tenant_has_another(db_session: AsyncSession) -> None:
    first = await _seed(db_session, load_tenant_spec(SPEC_YAML), widget_key="wk_local")
    # Тот же ключ при повторном seed — без предупреждения (make seed с SEED_WIDGET_KEY).
    same = await _seed(db_session, load_tenant_spec(SPEC_YAML), widget_key="wk_local")
    assert (same.new_widget_key, same.widget_key_ignored) == (None, False)
    # Другой ключ без reset_widget_key не применяется, и это видно в результате (T-7).
    other = await _seed(db_session, load_tenant_spec(SPEC_YAML), widget_key="wk_other")
    assert (other.new_widget_key, other.widget_key_ignored) == (None, True)
    [key] = await WidgetKeyRepository(db_session).list(first.tenant.id)
    assert key.key_hash == hash_widget_key("wk_local")
    # Без заданного ключа предупреждать не о чем.
    assert (await _seed(db_session, load_tenant_spec(SPEC_YAML))).widget_key_ignored is False


async def test_reset_widget_key_replaces_keys(db_session: AsyncSession) -> None:
    first = await _seed(db_session, load_tenant_spec(SPEC_YAML))
    reset = await seed_tenant(
        load_tenant_spec(SPEC_YAML),
        tenants=SqlTenantDirectory(db_session),
        configs=AgentConfigRepository(db_session),
        widget_keys=WidgetKeyRepository(db_session),
        widget_key="wk_new",
        reset_widget_key=True,
    )

    assert reset.new_widget_key == "wk_new"
    [key] = await WidgetKeyRepository(db_session).list(first.tenant.id)
    assert key.key_hash == hash_widget_key("wk_new")
