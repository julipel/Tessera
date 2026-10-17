"""Репозиторий заявок (P5-04a, ADR-0045): запись, номера на тенанта, страницы, изоляция
тенантов (ADR-0006) и `SqlLeadStore` на настоящих транзакциях."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.chat.public import ConversationRecord
from app.modules.leads.domain.lead import LeadCursor, LeadFilter
from app.modules.leads.public import Lead, LeadRepository, SqlLeadStore
from app.modules.shared.public import TenantId
from app.modules.tenants.public import AgentConfigRepository, SqlTenantDirectory, TenantRecord


async def _conversation(session: AsyncSession, slug: str) -> tuple[TenantId, UUID]:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    return tenant.id, await _another_conversation(session, tenant.id)


async def _another_conversation(session: AsyncSession, tenant_id: TenantId) -> UUID:
    config = await AgentConfigRepository(session).create_draft(tenant_id, {"assistant": {}})
    record = ConversationRecord(
        tenant_id=tenant_id, agent_config_id=config.id, channel="web", visitor_id="v"
    )
    session.add(record)
    await session.flush()
    return record.id


async def test_lead_is_stored_and_visible_only_to_own_tenant(db_session: AsyncSession) -> None:
    shop, shop_conversation = await _conversation(db_session, "shop")
    other, other_conversation = await _conversation(db_session, "other")
    repo = LeadRepository(db_session)

    lead = await repo.create(shop, shop_conversation, "contact", {"phone": "+7900"})
    await repo.create(other, other_conversation, "contact", {"phone": "+7911"})

    assert (lead.tenant_id, lead.form_key, lead.fields) == (shop, "contact", {"phone": "+7900"})
    assert await repo.list_for_conversation(shop, shop_conversation) == [lead]
    # Чужой тенант не видит заявок диалога, даже зная его id.
    assert await repo.list_for_conversation(other, shop_conversation) == []
    assert await repo.get(other, lead.id) is None


async def test_same_idempotency_key_returns_existing_lead(db_session: AsyncSession) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    repo = LeadRepository(db_session)

    first = await repo.create(
        shop, conversation, "contact", {"phone": "+7900"}, idempotency_key="cf_1"
    )
    again = await repo.create(
        shop, conversation, "contact", {"phone": "+7911"}, idempotency_key="cf_1"
    )
    other = await repo.create(
        shop, conversation, "contact", {"phone": "+7922"}, idempotency_key="cf_2"
    )

    # Повтор подтверждения cf_1 не создаёт вторую заявку и не меняет первую (P9-01g).
    assert again == first
    assert again.fields == {"phone": "+7900"}
    assert other.id != first.id
    # Одна транзакция — равный created_at, порядок задаёт id (REVIEW.md: L-10).
    assert await repo.list_for_conversation(shop, conversation) == sorted(
        [first, other], key=lambda lead: lead.id
    )


async def test_idempotency_key_is_scoped_to_conversation_and_tenant(
    db_session: AsyncSession,
) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    other, other_conversation = await _conversation(db_session, "other")
    second = await _another_conversation(db_session, shop)
    repo = LeadRepository(db_session)

    leads = [
        await repo.create(tenant, conv, "contact", {"phone": "+7900"}, idempotency_key="cf_1")
        for tenant, conv in [(shop, conversation), (shop, second), (other, other_conversation)]
    ]

    assert len({lead.id for lead in leads}) == 3


async def test_leads_without_key_are_not_deduplicated(db_session: AsyncSession) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    repo = LeadRepository(db_session)

    await repo.create(shop, conversation, "contact", {"phone": "+7900"})
    await repo.create(shop, conversation, "contact", {"phone": "+7900"})

    assert len(await repo.list_for_conversation(shop, conversation)) == 2


async def test_numbers_are_sequential_per_tenant(db_session: AsyncSession) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    other, other_conversation = await _conversation(db_session, "other")
    repo = LeadRepository(db_session)

    shop_numbers = [
        (await repo.create(shop, conversation, "contact", {"phone": "+7900"})).number
        for _ in range(3)
    ]
    other_lead = await repo.create(other, other_conversation, "contact", {"phone": "+7911"})

    # Счётчик у каждого тенанта свой (ADR-0045, REVIEW.md: L-14).
    assert shop_numbers == [1, 2, 3]
    assert other_lead.number == 1


async def test_repeat_with_same_key_does_not_spend_number(db_session: AsyncSession) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    repo = LeadRepository(db_session)

    first = await repo.create(shop, conversation, "c", {"phone": "+7900"}, idempotency_key="cf_1")
    await repo.create(shop, conversation, "c", {"phone": "+7900"}, idempotency_key="cf_1")
    second = await repo.create(shop, conversation, "c", {"phone": "+7900"}, idempotency_key="cf_2")

    assert (first.number, second.number) == (1, 2)


async def test_lead_keeps_form_labels_and_detached_fields(db_session: AsyncSession) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    lead = await LeadRepository(db_session).create(
        shop,
        conversation,
        "contact",
        {"phone": "+7900", "extra": "x"},
        form_title="Обратный звонок",
        field_labels={"phone": "Телефон"},
    )

    [stored] = await LeadRepository(db_session).list_for_conversation(shop, conversation)
    assert (stored.form_title, stored.label("phone"), stored.label("extra")) == (
        "Обратный звонок",
        "Телефон",
        "extra",  # подписи нет — ключ поля
    )
    # Не живой dict ORM-записи (REVIEW.md: L-12).
    assert isinstance(lead.fields, MappingProxyType)
    with pytest.raises(TypeError):
        lead.fields["phone"] = "+7000"  # type: ignore[index]


async def test_list_for_conversation_orders_by_created_at_then_id(
    db_session: AsyncSession,
) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    repo = LeadRepository(db_session)
    leads = [await repo.create(shop, conversation, "c", {"n": str(i)}) for i in range(3)]
    # Одна транзакция — один now(): порядок при равном created_at задаёт id (REVIEW.md: L-10).
    assert len({lead.created_at for lead in leads}) == 1

    listed = await repo.list_for_conversation(shop, conversation)

    assert listed == sorted(leads, key=lambda lead: lead.id)


async def test_list_page_filters_and_pages_without_gaps(db_session: AsyncSession) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    other, other_conversation = await _conversation(db_session, "other")
    repo = LeadRepository(db_session)
    created = [
        await repo.create(shop, conversation, "call" if i % 2 else "visit", {"n": str(i)})
        for i in range(5)
    ]
    await repo.create(other, other_conversation, "call", {"n": "x"})
    newest_first = sorted(created, key=lambda lead: (lead.created_at, lead.id), reverse=True)

    seen: list[Lead] = []
    cursor: LeadCursor | None = None
    while True:
        page = await repo.list_page(shop, before=cursor, limit=2, where=LeadFilter())
        seen += page
        if len(page) < 2:
            break
        cursor = LeadCursor(page[-1].created_at, page[-1].id)

    assert seen == newest_first
    calls = await repo.list_page(shop, before=None, limit=10, where=LeadFilter(form_key="call"))
    assert {lead.form_key for lead in calls} == {"call"} and len(calls) == 2
    by_number = await repo.list_page(shop, before=None, limit=10, where=LeadFilter(number=3))
    assert [lead.number for lead in by_number] == [3]
    # Чужой тенант по номеру заявки не находит.
    assert await repo.list_page(other, before=None, limit=10, where=LeadFilter(number=3)) == []
    later = created[0].created_at + timedelta(seconds=1)
    assert (
        await repo.list_page(shop, before=None, limit=10, where=LeadFilter(created_from=later))
        == []
    )
    assert (
        len(await repo.list_page(shop, before=None, limit=10, where=LeadFilter(created_to=later)))
        == 5
    )


async def test_sql_lead_store_commits_lead_and_repeats_are_idempotent(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    await db_session.commit()
    store = SqlLeadStore(
        async_sessionmaker(bind=db_connection, join_transaction_mode="create_savepoint")
    )

    first = await store.create(
        shop, conversation, "contact", {"phone": "+7900"}, **LABELS, idempotency_key="cf_1"
    )
    again = await store.create(
        shop, conversation, "contact", {"phone": "+7900"}, **LABELS, idempotency_key="cf_1"
    )

    assert again == first
    [lead] = await LeadRepository(db_session).list_for_conversation(shop, conversation)
    assert (lead.id, lead.number, lead.form_title) == (first.id, 1, "Связь")


# --- SqlLeadStore на настоящих транзакциях (REVIEW.md: L-7): свой commit, своя блокировка ---

LABELS: dict[str, Any] = {"form_title": "Связь", "field_labels": {"phone": "Телефон"}}


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def committed_conversation(engine: AsyncEngine) -> AsyncIterator[tuple[TenantId, UUID]]:
    """Тенант и диалог вне транзакции теста; удаляются каскадом вместе с заявками."""
    async with AsyncSession(engine) as session:
        tenant_id, conversation_id = await _conversation(session, f"leads-{uuid4().hex[:8]}")
        await session.commit()
    yield tenant_id, conversation_id
    async with AsyncSession(engine) as session:
        await session.execute(delete(TenantRecord).where(TenantRecord.id == tenant_id))
        await session.commit()


async def test_lead_survives_rollback_of_turn_session(
    engine: AsyncEngine, committed_conversation: tuple[TenantId, UUID]
) -> None:
    tenant_id, conversation_id = committed_conversation
    store = SqlLeadStore(async_sessionmaker(engine))

    # Ход открыл свою транзакцию, инструмент создал заявку, затем ход прервался и откатился.
    async with AsyncSession(engine) as turn:
        await turn.execute(select(TenantRecord.id).where(TenantRecord.id == tenant_id))
        created = await store.create(
            tenant_id,
            conversation_id,
            "contact",
            {"phone": "+7900"},
            **LABELS,
            idempotency_key="cf_1",
        )
        await turn.rollback()

    async with AsyncSession(engine) as session:
        [lead] = await LeadRepository(session).list_for_conversation(tenant_id, conversation_id)
    assert (lead.id, lead.number) == (created.id, created.number)


async def test_parallel_leads_get_distinct_numbers(
    engine: AsyncEngine, committed_conversation: tuple[TenantId, UUID]
) -> None:
    tenant_id, conversation_id = committed_conversation
    store = SqlLeadStore(async_sessionmaker(engine))

    created = await asyncio.gather(
        *(
            store.create(
                tenant_id,
                conversation_id,
                "contact",
                {"phone": "+7900"},
                **LABELS,
                idempotency_key=f"cf_{i}",
            )
            for i in range(8)
        )
    )

    assert sorted(lead.number for lead in created) == list(range(1, 9))


async def test_parallel_repeats_of_one_confirmation_give_one_lead(
    engine: AsyncEngine, committed_conversation: tuple[TenantId, UUID]
) -> None:
    tenant_id, conversation_id = committed_conversation
    store = SqlLeadStore(async_sessionmaker(engine))

    created = await asyncio.gather(
        *(
            store.create(
                tenant_id,
                conversation_id,
                "contact",
                {"phone": "+7900"},
                **LABELS,
                idempotency_key="cf_1",
            )
            for _ in range(4)
        )
    )

    assert len({lead.id for lead in created}) == 1
    async with AsyncSession(engine) as session:
        leads = await LeadRepository(session).list_for_conversation(tenant_id, conversation_id)
    assert len(leads) == 1
