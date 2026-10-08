"""Версии AgentConfig в админке (P8-02a): YAML ↔ JSON, валидация, черновик → активная, откат."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import AgentConfigVersionDetail, AgentConfigVersionList, HttpError
from app.modules.shared.public import AdminRole, TenantId
from app.modules.tenants.application.config_editor import (
    agent_config_yaml,
    parse_agent_config_yaml,
)
from app.modules.tenants.domain.errors import InvalidAgentConfigError
from app.modules.tenants.public import (
    AgentConfigRepository,
    SqlTenantDirectory,
    WidgetKeyRepository,
    hash_widget_key,
    load_tenant_spec,
)

AdminLogin = Callable[..., Awaitable[dict[str, str]]]  # фикстура admin_login из conftest
TENANTS_DIR = Path(__file__).parents[3] / "config" / "tenants"


def _config(greeting: str = "Привет!") -> dict[str, Any]:
    return {
        "assistant": {"name": "Магазин", "greeting": greeting, "fallback_message": "Не вышло."},
        "model": {"primary": {"provider": "openai", "name": "test-model"}},
        "limits": {},
        "prompt": {"tenant": "Ты — консультант."},
        "tools": {},
    }


def _yaml(config: dict[str, Any]) -> str:
    return yaml.safe_dump(config, allow_unicode=True)


async def _tenant(session: AsyncSession, slug: str) -> TenantId:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    await WidgetKeyRepository(session).add_key(tenant.id, hash_widget_key(f"wk_{slug}"), [])
    configs = AgentConfigRepository(session)
    draft = await configs.create_draft(tenant.id, _config())
    await configs.activate(tenant.id, draft.id)
    return tenant.id


def _url(tenant_id: TenantId, suffix: str = "") -> str:
    return f"/v1/admin/tenants/{tenant_id}/agent-configs{suffix}"


@pytest.fixture
async def shop(db_session: AsyncSession) -> TenantId:
    return await _tenant(db_session, "shop")


@pytest.fixture
async def editor(admin_login: AdminLogin, shop: TenantId) -> dict[str, str]:
    return await admin_login("editor@example.com", roles={shop: AdminRole.EDITOR})


async def _versions(client: AsyncClient, tenant_id: TenantId, headers: dict[str, str]) -> list[Any]:
    response = await client.get(_url(tenant_id), headers=headers)
    assert response.status_code == 200, response.text
    return AgentConfigVersionList.model_validate(response.json()).versions


# --- YAML ↔ JSON ---


@pytest.mark.parametrize("path", sorted(TENANTS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_yaml_round_trip_keeps_config(path: Path) -> None:
    config = load_tenant_spec(path.read_text(encoding="utf-8")).config_json()
    text = agent_config_yaml(config)
    assert parse_agent_config_yaml(text) == config
    # Порядок ключей — как в AgentConfig, а не как вернул JSONB.
    assert text.startswith("assistant:")


@pytest.mark.parametrize(
    ("source", "loc", "message"),
    [
        ("assistant: [", (), "YAML: строка 1"),
        ("- 1\n- 2", (), "ожидается объект AgentConfig"),
        (_yaml({**_config(), "limits": {"max_steps": 0}}), ("limits", "max_steps"), ""),
        (_yaml({**_config(), "unknown": 1}), ("unknown",), ""),
        (
            _yaml(
                {
                    **_config(),
                    "prompt": {
                        "tenant": "x",
                        "scenarios": [
                            {"key": "a", "description": "a", "instructions": "a"},
                            {"key": "a", "description": "b", "instructions": "b"},
                        ],
                    },
                }
            ),
            ("prompt", "scenarios"),
            "повторяются ключи сценариев: a",
        ),
    ],
    ids=["broken-yaml", "not-object", "schema", "extra-key", "duplicate-scenario"],
)
def test_invalid_yaml_reports_where(source: str, loc: tuple[Any, ...], message: str) -> None:
    with pytest.raises(InvalidAgentConfigError) as raised:
        parse_agent_config_yaml(source)
    assert any(p.loc == loc and message in p.message for p in raised.value.problems), (
        raised.value.problems
    )


def test_stored_config_has_no_defaults() -> None:
    # Как seed: значения по умолчанию применяются при чтении, в БД — как написано.
    assert parse_agent_config_yaml(_yaml(_config())) == _config()


# --- доступ ---


async def test_requires_session(db_client: AsyncClient, shop: TenantId) -> None:
    response = await db_client.get(_url(shop))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_viewer_and_other_tenant_editor_forbidden(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin, shop: TenantId
) -> None:
    other = await _tenant(db_session, "other")
    viewer = await admin_login("viewer@example.com", roles={shop: AdminRole.VIEWER})
    stranger = await admin_login("stranger@example.com", roles={other: AdminRole.EDITOR})
    for headers in (viewer, stranger):
        for response in (
            await db_client.get(_url(shop), headers=headers),
            await db_client.post(_url(shop), json={"yaml": _yaml(_config())}, headers=headers),
        ):
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "forbidden"


async def test_superadmin_allowed(
    db_client: AsyncClient, admin_login: AdminLogin, shop: TenantId
) -> None:
    root = await admin_login("root@example.com", superadmin=True)
    assert [v.status for v in await _versions(db_client, shop, root)] == ["active"]


# --- черновик, активация, откат ---


async def test_save_draft_does_not_change_active(
    db_client: AsyncClient, editor: dict[str, str], shop: TenantId
) -> None:
    response = await db_client.post(
        _url(shop), json={"yaml": _yaml(_config("Здравствуйте!"))}, headers=editor
    )
    assert response.status_code == 201, response.text
    draft = AgentConfigVersionDetail.model_validate(response.json())
    assert (draft.version, draft.status) == (2, "draft")
    assert "Здравствуйте!" in draft.yaml

    versions = await _versions(db_client, shop, editor)
    assert [(v.version, v.status) for v in versions] == [(2, "draft"), (1, "active")]
    public = await db_client.get("/v1/public/config", headers={"X-Widget-Key": "wk_shop"})
    assert public.json()["assistant"]["greeting"] == "Привет!"

    response = await db_client.get(_url(shop, f"/{draft.id}"), headers=editor)
    assert AgentConfigVersionDetail.model_validate(response.json()) == draft


async def test_invalid_draft_is_422_with_details(
    db_client: AsyncClient, editor: dict[str, str], shop: TenantId
) -> None:
    bad = _yaml({**_config(), "limits": {"max_steps": 0}})
    response = await db_client.post(_url(shop), json={"yaml": bad}, headers=editor)
    assert response.status_code == 422
    error = HttpError.model_validate(response.json()).error
    assert error.code == "invalid_input"
    assert error.details and error.details[0].loc == ["limits", "max_steps"]
    assert [v.version for v in await _versions(db_client, shop, editor)] == [1]


async def test_activate_draft_then_roll_back(
    db_client: AsyncClient, editor: dict[str, str], shop: TenantId
) -> None:
    async def greeting() -> str:
        response = await db_client.get("/v1/public/config", headers={"X-Widget-Key": "wk_shop"})
        return str(response.json()["assistant"]["greeting"])

    first = (await _versions(db_client, shop, editor))[0]
    created = await db_client.post(
        _url(shop), json={"yaml": _yaml(_config("Здравствуйте!"))}, headers=editor
    )
    draft_id = created.json()["id"]

    response = await db_client.post(_url(shop, f"/{draft_id}/activate"), headers=editor)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert [v.status for v in await _versions(db_client, shop, editor)] == ["active", "archived"]
    assert await greeting() == "Здравствуйте!"

    # Откат — активация архивной версии; повторная активация ничего не меняет.
    for _ in range(2):
        response = await db_client.post(_url(shop, f"/{first.id}/activate"), headers=editor)
        assert response.status_code == 200, response.text
    assert [v.status for v in await _versions(db_client, shop, editor)] == ["archived", "active"]
    assert await greeting() == "Привет!"


async def test_activate_rechecks_stored_config(
    db_client: AsyncClient, db_session: AsyncSession, editor: dict[str, str], shop: TenantId
) -> None:
    # Версия, сохранённая до изменения схемы, могла стать невалидной.
    stale = await AgentConfigRepository(db_session).create_draft(
        shop, {**_config(), "removed_field": True}
    )
    response = await db_client.post(_url(shop, f"/{stale.id}/activate"), headers=editor)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["removed_field"]
    assert [v.status for v in await _versions(db_client, shop, editor)] == ["draft", "active"]


async def test_other_tenant_version_not_found(
    db_client: AsyncClient, db_session: AsyncSession, editor: dict[str, str], shop: TenantId
) -> None:
    other = await _tenant(db_session, "other")
    foreign = (await AgentConfigRepository(db_session).list_versions(other))[0]
    for config_id in (foreign.id, uuid4()):
        get = await db_client.get(_url(shop, f"/{config_id}"), headers=editor)
        activate = await db_client.post(_url(shop, f"/{config_id}/activate"), headers=editor)
        assert (get.status_code, activate.status_code) == (404, 404)
    assert (await AgentConfigRepository(db_session).get_active(other)) == foreign
