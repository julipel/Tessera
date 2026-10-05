"""Seed тенанта из YAML: валидация описания и идемпотентность."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tenants.public import (
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
    session: AsyncSession, spec: TenantSpec, widget_key: str | None = None
) -> SeedResult:
    return await seed_tenant(
        spec,
        tenants=SqlTenantDirectory(session),
        configs=AgentConfigRepository(session),
        widget_keys=WidgetKeyRepository(session),
        widget_key=widget_key,
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


async def test_seed_renames_tenant(db_session: AsyncSession) -> None:
    await _seed(db_session, load_tenant_spec(SPEC_YAML))
    await _seed(db_session, load_tenant_spec(SPEC_YAML.replace('name: "Shop"', 'name: "Shop 2"')))

    tenant = await SqlTenantDirectory(db_session).get_by_slug("shop")
    assert tenant is not None
    assert tenant.name == "Shop 2"


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
