"""GET /v1/public/config: аутентификация по X-Widget-Key и публичная часть конфига."""

from typing import Any

import pytest
import structlog
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import HttpError, PublicConfig, WidgetEmbed
from app.logs import TRACE_ID_HEADER
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
WIDGET_URL = "/v1/public/widget"
SHOP_SITE = "https://shop.example"
CHAT_ORIGIN = "http://localhost:3000"  # Settings.cors_origins по умолчанию — свой веб-чат


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
    session: AsyncSession,
    slug: str,
    key: str,
    config: dict[str, Any] | None,
    allowed_origins: list[str] | None = None,
) -> TenantId:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    await WidgetKeyRepository(session).add_key(
        tenant.id, hash_widget_key(key), allowed_origins or []
    )
    if config is not None:
        configs = AgentConfigRepository(session)
        draft = await configs.create_draft(tenant.id, config)
        await configs.activate(tenant.id, draft.id)
    return tenant.id


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    return await _tenant(
        db_session,
        "shop",
        "wk_shop",
        _config("Shop", logo_url="/shop.svg"),
        allowed_origins=[SHOP_SITE],
    )


async def test_returns_public_part_of_active_config(db_client: AsyncClient, shop: TenantId) -> None:
    response = await db_client.get(URL, headers={"X-Widget-Key": "wk_shop"})

    assert response.status_code == 200
    body = response.json()
    config = PublicConfig.model_validate(body)
    assert config.assistant.name == "Shop"
    assert config.assistant.language == "ru"  # auto без locale — default_language
    assert config.assistant.starter_suggestions == ["Подобрать"]
    assert config.branding.tokens is not None
    assert config.branding.tokens.primary == "#123456"
    assert config.branding.logo_url == "/shop.svg"
    assert set(body) == {"assistant", "branding"}
    assert "СЕКРЕТНЫЙ ПРОМПТ" not in response.text
    assert "fallback_message" not in body["assistant"]


async def test_locale_selects_language_and_translated_texts(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    config = _config("Shop")
    config["assistant"]["translations"] = {
        "en": {"greeting": "Hi from Shop", "starter_suggestions": ["Find a gift"]}
    }
    await _tenant(db_session, "intl", "wk_intl", config)

    response = await db_client.get(
        URL, params={"locale": "en-US"}, headers={"X-Widget-Key": "wk_intl"}
    )

    assert response.status_code == 200
    assistant = PublicConfig.model_validate(response.json()).assistant
    assert (assistant.language, assistant.greeting) == ("en", "Hi from Shop")
    assert assistant.starter_suggestions == ["Find a gift"]


@pytest.mark.parametrize("headers", [{}, {"X-Widget-Key": ""}, {"X-Widget-Key": "wk_unknown"}])
async def test_missing_or_unknown_key_is_401(
    db_client: AsyncClient, shop: TenantId, headers: dict[str, str]
) -> None:
    response = await db_client.get(URL, headers=headers)

    assert response.status_code == 401
    assert HttpError.model_validate(response.json()).error.code == "unauthorized"


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
    assert HttpError.model_validate(response.json()).error.code == "not_found"


async def test_key_selects_its_own_tenant(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    await _tenant(db_session, "other", "wk_other", _config("Other"))

    shop_response = await db_client.get(URL, headers={"X-Widget-Key": "wk_shop"})
    other_response = await db_client.get(URL, headers={"X-Widget-Key": "wk_other"})

    assert shop_response.json()["assistant"]["name"] == "Shop"
    assert other_response.json()["assistant"]["name"] == "Other"


async def test_logs_carry_tenant_id_from_widget_key(db_client: AsyncClient, shop: TenantId) -> None:
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        response = await db_client.get(
            URL, headers={"X-Widget-Key": "wk_shop", TRACE_ID_HEADER: "trace-1"}
        )

    assert response.status_code == 200
    [entry] = [e for e in logs if e["event"] == "public_config_served"]
    assert entry["tenant_id"] == str(shop)
    assert entry["trace_id"] == "trace-1"


async def test_widget_embed_returns_allowed_origins(db_client: AsyncClient, shop: TenantId) -> None:
    response = await db_client.get(WIDGET_URL, headers={"X-Widget-Key": "wk_shop"})

    assert response.status_code == 200
    assert WidgetEmbed.model_validate(response.json()).allowed_origins == [SHOP_SITE]


async def test_widget_embed_without_origins_is_empty(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _tenant(db_session, "closed", "wk_closed", _config("Closed"))

    response = await db_client.get(WIDGET_URL, headers={"X-Widget-Key": "wk_closed"})

    assert response.json() == {"allowed_origins": []}


async def test_widget_embed_unknown_key_is_401(db_client: AsyncClient, shop: TenantId) -> None:
    response = await db_client.get(WIDGET_URL, headers={"X-Widget-Key": "wk_unknown"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "origin",
    [SHOP_SITE, CHAT_ORIGIN, None],
    ids=["tenant-site", "own-chat", "no-origin"],
)
async def test_allowed_origin_passes(
    db_client: AsyncClient, shop: TenantId, origin: str | None
) -> None:
    headers = {"X-Widget-Key": "wk_shop"} | ({"Origin": origin} if origin else {})

    response = await db_client.get(URL, headers=headers)

    assert response.status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "https://shop.example:8443", "null"])
async def test_foreign_origin_is_403(db_client: AsyncClient, shop: TenantId, origin: str) -> None:
    response = await db_client.get(URL, headers={"X-Widget-Key": "wk_shop", "Origin": origin})

    assert response.status_code == 403
    assert HttpError.model_validate(response.json()).error.code == "forbidden"


async def test_origin_of_other_tenant_is_403(
    db_client: AsyncClient, db_session: AsyncSession, shop: TenantId
) -> None:
    # allowed_origins — свойство ключа: сайт одного тенанта не открывает доступ к другому.
    await _tenant(db_session, "other", "wk_other", _config("Other"), ["https://other.example"])

    response = await db_client.get(
        URL, headers={"X-Widget-Key": "wk_shop", "Origin": "https://other.example"}
    )

    assert response.status_code == 403


async def test_chat_api_checks_origin_too(db_client: AsyncClient, shop: TenantId) -> None:
    # Проверка — в общей зависимости ключа виджета, ей подчиняется и API диалогов.
    response = await db_client.post(
        "/v1/conversations",
        json={"visitor_id": "v1"},
        headers={"X-Widget-Key": "wk_shop", "Origin": "https://evil.example"},
    )

    assert response.status_code == 403
