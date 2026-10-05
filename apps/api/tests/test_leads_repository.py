"""Репозиторий заявок (P5-04a): запись и изоляция тенантов (ADR-0006)."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.public import ConversationRecord
from app.modules.leads.public import LeadRepository
from app.modules.shared.public import TenantId
from app.modules.tenants.public import AgentConfigRepository, SqlTenantDirectory


async def _conversation(session: AsyncSession, slug: str) -> tuple[TenantId, UUID]:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    config = await AgentConfigRepository(session).create_draft(tenant.id, {"assistant": {}})
    record = ConversationRecord(
        tenant_id=tenant.id, agent_config_id=config.id, channel="web", visitor_id="v"
    )
    session.add(record)
    await session.flush()
    return tenant.id, record.id


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
