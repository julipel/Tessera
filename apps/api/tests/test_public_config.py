"""GET /v1/public/config: аутентификация по X-Widget-Key и публичная часть конфига."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import PublicConfig
from app.modules.shared.public import TenantId
from app.modules.tenants.public import (
    AgentConfigRepository,
    SqlTenantDirectory,
    TenantRecord,
    TenantStatus,
    WidgetKeyRepository,
    hash_widget_key,
)

URL = "/v1/public/config"


def _config(name: str, **branding: Any) -> dict[str, Any]:
    return {
        "assistant": {
            "name": name,
            "greeting": f"Привет от {name}",
            "starter_suggestions": ["Подобрать"],
            "fallback_message": "Ошибка",
        },
        "model": {"primary": {"provider": "openai", "name": "m"}},
        "limits": {},
        "prompt": {"tenant": "СЕКРЕТНЫЙ ПРОМПТ"},
        "tools": {"builtin": ["search_catalog"]},
        "branding": {"tokens": {"primary": "#123456"}, **branding},
    }


async def _tenant(
    session: AsyncSession, slug: str, key: str, config: dict[str, Any] | None
) -> TenantId:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    await WidgetKeyRepository(session).add_key(tenant.id, hash_widget_key(key), [])
    if config is not None:
        configs = AgentConfigRepository(session)
        draft = await configs.create_draft(tenant.id, config)
        await configs.activate(tenant.id, draft.id)
    return tenant.id


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    return await _tenant(db_session, "shop", "wk_shop", _config("Shop"))


async def test_returns_public_part_of_active_config(db_client: AsyncClient, shop: TenantId) -> None:
    response = await db_client.get(URL, headers={"X-Widget-Key": "wk_shop"})

    assert response.status_code == 200
    body = response.json()
    config = PublicConfig.model_validate(body)
    assert config.assistant.name == "Shop"
    assert config.assistant.language == "auto"
    assert config.assistant.starter_suggestions == ["Подобрать"]
    assert config.branding.tokens is not None
    assert config.branding.tokens.primary == "#123456"
    assert set(body) == {"assistant", "branding"}
    assert "СЕКРЕТНЫЙ ПРОМПТ" not in response.text
    assert "fallback_message" not in body["assistant"]


@pytest.mark.parametrize("headers", [{}, {"X-Widget-Key": ""}, {"X-Widget-Key": "wk_unknown"}])
async def test_missing_or_unknown_key_is_401(
    db_client: AsyncClient, shop: TenantId, headers: dict[str, str]
) -> None:
    response = await db_client.get(URL, headers=headers)

    assert response.status_code == 401


async def test_disabled_tenant_key_is_401(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    await db_session.execute(
        update(TenantRecord).where(TenantRecord.id == shop).values(status=TenantStatus.DISABLED)
    )

    response = await db_client.get(URL, headers={"X-Widget-Key": "wk_shop"})

    assert response.status_code == 401


async def test_tenant_without_active_config_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _tenant(db_session, "empty", "wk_empty", config=None)

    response = await db_client.get(URL, headers={"X-Widget-Key": "wk_empty"})

    assert response.status_code == 404


async def test_key_selects_its_own_tenant(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    await _tenant(db_session, "other", "wk_other", _config("Other"))

    shop_response = await db_client.get(URL, headers={"X-Widget-Key": "wk_shop"})
    other_response = await db_client.get(URL, headers={"X-Widget-Key": "wk_other"})

    assert shop_response.json()["assistant"]["name"] == "Shop"
    assert other_response.json()["assistant"]["name"] == "Other"
