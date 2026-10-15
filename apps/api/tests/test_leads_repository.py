"""Репозиторий заявок (P5-04a): запись и изоляция тенантов (ADR-0006)."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.modules.chat.public import ConversationRecord
from app.modules.leads.public import LeadRepository, SqlLeadStore
from app.modules.shared.public import TenantId
from app.modules.tenants.public import AgentConfigRepository, SqlTenantDirectory


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
    assert await repo.list_for_conversation(shop, conversation) == [first, other]


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


async def test_sql_lead_store_commits_lead_and_repeats_are_idempotent(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    shop, conversation = await _conversation(db_session, "shop")
    await db_session.commit()
    store = SqlLeadStore(
        async_sessionmaker(bind=db_connection, join_transaction_mode="create_savepoint")
    )

    first = await store.create(
        shop, conversation, "contact", {"phone": "+7900"}, idempotency_key="cf_1"
    )
    again = await store.create(
        shop, conversation, "contact", {"phone": "+7900"}, idempotency_key="cf_1"
    )

    assert again == first
    [lead] = await LeadRepository(db_session).list_for_conversation(shop, conversation)
    assert lead.id == first
