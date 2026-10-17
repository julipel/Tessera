"""Заявки в админке (ADR-0045): список страницами, фильтры, выгрузка CSV, роли и изоляция
тенантов."""

import csv
import io
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import LeadList
from app.modules.chat.domain.entities import Channel
from app.modules.chat.public import ConversationRepository
from app.modules.leads.application.admin import decode_cursor, encode_cursor, leads_csv
from app.modules.leads.domain.errors import InvalidCursorError
from app.modules.leads.domain.lead import Lead, LeadCursor
from app.modules.leads.public import LeadRepository
from app.modules.shared.public import AdminRole, TenantId
from app.modules.tenants.public import AgentConfigRepository, SqlTenantDirectory

AdminLogin = Callable[..., Awaitable[dict[str, str]]]  # фикстура admin_login из conftest

LABELS = {"name": "Имя", "phone": "Телефон"}


async def _shop(session: AsyncSession, slug: str) -> tuple[TenantId, UUID]:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    draft = await AgentConfigRepository(session).create_draft(tenant.id, {"assistant": {}})
    conversation = await ConversationRepository(session).create(
        tenant.id, draft.id, Channel.WEB, "visitor", language="ru"
    )
    return tenant.id, conversation.id


async def _lead(
    session: AsyncSession,
    tenant_id: TenantId,
    conversation_id: UUID,
    fields: dict[str, str],
    form_key: str = "callback",
) -> Lead:
    return await LeadRepository(session).create(
        tenant_id,
        conversation_id,
        form_key,
        fields,
        form_title="Обратный звонок",
        field_labels=LABELS,
    )


def _url(tenant_id: TenantId, tail: str = "leads") -> str:
    return f"/v1/admin/tenants/{tenant_id}/{tail}"


async def test_list_returns_leads_with_labels_newest_first(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin
) -> None:
    shop, conversation = await _shop(db_session, "shop")
    first = await _lead(db_session, shop, conversation, {"name": "Анна", "phone": "+7900"})
    second = await _lead(db_session, shop, conversation, {"name": "Иван", "extra": "x"})
    headers = await admin_login(roles={shop: AdminRole.VIEWER})

    response = await db_client.get(_url(shop), headers=headers)

    assert response.status_code == 200, response.text
    page = LeadList.model_validate(response.json())
    # Одна транзакция — равный created_at; порядок задаёт id.
    expected = sorted([first, second], key=lambda lead: lead.id, reverse=True)
    assert [lead.id for lead in page.leads] == [lead.id for lead in expected]
    assert page.next_before is None
    item = next(lead for lead in page.leads if lead.id == second.id)
    assert (item.number, item.form_title, item.conversation_id) == (
        2,
        "Обратный звонок",
        conversation,
    )
    assert [(f.name, f.label, f.value) for f in item.fields] == [
        ("name", "Имя", "Иван"),
        ("extra", "extra", "x"),  # подписи нет — ключ поля
    ]


async def test_list_pages_filters_by_form_and_number(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin
) -> None:
    shop, conversation = await _shop(db_session, "shop")
    for i in range(5):
        await _lead(db_session, shop, conversation, {"n": str(i)}, "call" if i % 2 else "visit")
    headers = await admin_login(roles={shop: AdminRole.VIEWER})

    numbers: list[int] = []
    params: dict[str, Any] = {"limit": 2}
    while True:
        page = LeadList.model_validate(
            (await db_client.get(_url(shop), params=params, headers=headers)).json()
        )
        numbers += [lead.number for lead in page.leads]
        if page.next_before is None:
            break
        params["before"] = page.next_before

    assert sorted(numbers) == [1, 2, 3, 4, 5] and len(numbers) == 5
    calls = (await db_client.get(_url(shop), params={"form_key": "call"}, headers=headers)).json()
    assert sorted(lead["number"] for lead in calls["leads"]) == [2, 4]
    found = (await db_client.get(_url(shop), params={"number": 3}, headers=headers)).json()
    assert [lead["number"] for lead in found["leads"]] == [3]


async def test_other_tenant_and_missing_role_are_forbidden(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin
) -> None:
    shop, conversation = await _shop(db_session, "shop")
    other, _ = await _shop(db_session, "other")
    await _lead(db_session, shop, conversation, {"phone": "+7900"})
    headers = await admin_login(roles={other: AdminRole.EDITOR})

    for tail in ("leads", "leads.csv"):
        assert (await db_client.get(_url(shop, tail), headers=headers)).status_code == 403
        assert (await db_client.get(_url(shop, tail))).status_code == 401
    own = (await db_client.get(_url(other), headers=headers)).json()
    assert own["leads"] == []


@pytest.mark.parametrize(
    "before",
    [
        "garbage",
        encode_cursor(LeadCursor(datetime(2026, 1, 1), uuid4())),
        # Край календаря: в UTC не переводится — 422, а не 500 от asyncpg.
        encode_cursor(LeadCursor(datetime.fromisoformat("0001-01-01T00:00:00+14:00"), uuid4())),
    ],
)
async def test_invalid_cursor_is_422(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin, before: str
) -> None:
    shop, _ = await _shop(db_session, "shop")
    headers = await admin_login(roles={shop: AdminRole.VIEWER})

    response = await db_client.get(_url(shop), params={"before": before}, headers=headers)

    assert response.status_code == 422


def test_cursor_roundtrip() -> None:
    cursor = LeadCursor(datetime(2026, 10, 17, 12, tzinfo=UTC), uuid4())
    assert decode_cursor(encode_cursor(cursor)) == cursor
    with pytest.raises(InvalidCursorError):
        decode_cursor("бяка")


async def test_csv_export_for_excel_with_period_and_log(
    db_client: AsyncClient,
    db_session: AsyncSession,
    admin_login: AdminLogin,
    debug_logs: list[MutableMapping[str, Any]],
) -> None:
    shop, conversation = await _shop(db_session, "shop")
    lead = await _lead(db_session, shop, conversation, {"name": "=HYPERLINK(1)", "phone": "+7900"})
    headers = await admin_login(roles={shop: AdminRole.VIEWER})
    since = (lead.created_at - timedelta(minutes=1)).isoformat()

    response = await db_client.get(_url(shop, "leads.csv"), params={"from": since}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith("\ufeff".encode())
    rows = list(csv.reader(io.StringIO(response.text.removeprefix("\ufeff")), delimiter=";"))
    assert rows == [
        ["Номер", "Создана (UTC)", "Форма", "Диалог", "Имя", "Телефон"],
        [
            "1",
            lead.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "Обратный звонок",
            str(conversation),
            "'=HYPERLINK(1)",  # не формула в Excel
            "'+7900",
        ],
    ]
    [log] = [e for e in debug_logs if e["event"] == "admin.leads_exported"]
    assert log["rows"] == 1
    assert "+7900" not in str(log)

    later = (lead.created_at + timedelta(minutes=1)).isoformat()
    empty = await db_client.get(_url(shop, "leads.csv"), params={"from": later}, headers=headers)
    assert empty.text.removeprefix("\ufeff").splitlines() == ["Номер;Создана (UTC);Форма;Диалог"]


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-10-17T00:00:00"},  # без пояса
        {"from": "2026-10-17T00:00:00+00:00", "to": "2026-10-16T00:00:00+00:00"},
        {"from": "0001-01-01T00:00:00+14:00"},  # край календаря
    ],
)
async def test_csv_rejects_bad_period(
    db_client: AsyncClient,
    db_session: AsyncSession,
    admin_login: AdminLogin,
    params: dict[str, str],
) -> None:
    shop, _ = await _shop(db_session, "shop")
    headers = await admin_login(roles={shop: AdminRole.VIEWER})

    response = await db_client.get(_url(shop, "leads.csv"), params=params, headers=headers)

    assert response.status_code == 422


async def test_csv_over_limit_is_422_not_truncated(
    db_client: AsyncClient,
    db_session: AsyncSession,
    admin_login: AdminLogin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.modules.leads.application.admin.MAX_EXPORT", 2)
    shop, conversation = await _shop(db_session, "shop")
    for i in range(3):
        await _lead(db_session, shop, conversation, {"n": str(i)})
    headers = await admin_login(roles={shop: AdminRole.VIEWER})

    response = await db_client.get(_url(shop, "leads.csv"), headers=headers)

    assert response.status_code == 422
    assert "сузьте период" in response.json()["error"]["message"]


def _bare_lead(number: int, fields: dict[str, str], labels: dict[str, str]) -> Lead:
    return Lead(
        id=uuid4(),
        tenant_id=TenantId(uuid4()),
        conversation_id=None,
        number=number,
        form_key="contact",
        fields=fields,
        created_at=datetime(2026, 10, 17, 15, 30, tzinfo=UTC),
        field_labels=labels,
    )


def test_csv_columns_union_in_first_seen_order_and_injection_guard() -> None:
    leads = [
        _bare_lead(2, {"phone": "-1", "city": "Москва"}, {"phone": "Телефон"}),
        _bare_lead(1, {"email": "@a", "phone": "\tx"}, {"email": "=Почта"}),
    ]

    rows = list(csv.reader(io.StringIO(leads_csv(leads).removeprefix("\ufeff")), delimiter=";"))

    assert rows[0] == ["Номер", "Создана (UTC)", "Форма", "Диалог", "Телефон", "city", "'=Почта"]
    # Диалог удалён по сроку — пусто; формы без заголовка — ключ; нет поля — пусто.
    assert rows[1] == ["2", "2026-10-17 15:30:00", "contact", "", "'-1", "Москва", ""]
    assert rows[2] == ["1", "2026-10-17 15:30:00", "contact", "", "'\tx", "", "'@a"]


async def test_fields_follow_form_order_not_jsonb_order(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin
) -> None:
    """Postgres хранит ключи JSONB-объекта по длине, а поля выводятся в порядке формы."""
    shop, conversation = await _shop(db_session, "shop")
    title = "Т" * 500  # у заголовка формы в конфиге нет предела длины
    await LeadRepository(db_session).create(
        shop,
        conversation,
        "callback",
        {"comment": "позже", "name": "Анна", "extra": "x"},
        form_title=title,
        field_labels={"comment": "Комментарий", "name": "Имя"},
    )
    db_session.expire_all()  # читать из БД, а не из сессии
    headers = await admin_login(roles={shop: AdminRole.VIEWER})

    [item] = (await db_client.get(_url(shop), headers=headers)).json()["leads"]
    csv_text = (await db_client.get(_url(shop, "leads.csv"), headers=headers)).text

    assert item["form_title"] == title
    assert [f["name"] for f in item["fields"]] == ["comment", "name", "extra"]
    header = csv_text.removeprefix("\ufeff").splitlines()[0]
    assert header == "Номер;Создана (UTC);Форма;Диалог;Комментарий;Имя;extra"


@pytest.mark.parametrize("params", [{"limit": 101}, {"limit": 0}, {"number": 0}, {"number": 2**31}])
async def test_list_rejects_out_of_range_params(
    db_client: AsyncClient,
    db_session: AsyncSession,
    admin_login: AdminLogin,
    params: dict[str, int],
) -> None:
    shop, _ = await _shop(db_session, "shop")
    headers = await admin_login(roles={shop: AdminRole.VIEWER})

    response = await db_client.get(_url(shop), params=params, headers=headers)

    assert response.status_code == 422


async def test_csv_period_end_is_exclusive_and_form_filter(
    db_client: AsyncClient, db_session: AsyncSession, admin_login: AdminLogin
) -> None:
    shop, conversation = await _shop(db_session, "shop")
    call = await _lead(db_session, shop, conversation, {"name": "Анна"}, "call")
    await _lead(db_session, shop, conversation, {"name": "Иван"}, "visit")
    headers = await admin_login(roles={shop: AdminRole.VIEWER})
    # Заявки из одной транзакции — с равным created_at.
    at = call.created_at.isoformat()

    async def numbers(params: dict[str, str]) -> list[str]:
        response = await db_client.get(_url(shop, "leads.csv"), params=params, headers=headers)
        assert response.status_code == 200, response.text
        rows = response.text.removeprefix("\ufeff").splitlines()[1:]
        return sorted(row.split(";")[0] for row in rows)

    # `to` не включается: заявка ровно на границе в выгрузку не попадает, `from` — включается.
    assert await numbers({"to": at}) == []
    assert await numbers({"from": at}) == ["1", "2"]
    assert await numbers({"form_key": "call"}) == [str(call.number)]
