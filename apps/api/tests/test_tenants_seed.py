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
