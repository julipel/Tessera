"""Чистка по срокам хранения для всех тенантов (ADR-0043): задача воркера и `make retention`.

Сборка из публичных интерфейсов модулей (как `knowledge_wiring.py`): репозитории chat,
leads и observability, политика и порции — `chat.application.retention`.

Каждый тенант — своя сессия; сроки берутся из активного конфига. Сбой одного тенанта не
останавливает остальных: лог `retention.failed`, тенант попадает в `failed`. `dry_run`
только считает (`SELECT count(*)` с теми же условиями) — без транзакции удаления и блокировок.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.public import (
    ConversationRepository,
    PurgeResult,
    count_expired,
    purge_expired,
    retention_policy,
)
from app.modules.leads.public import LeadRepository
from app.modules.observability.public import AgentEventRepository
from app.modules.shared.public import TenantId
from app.modules.tenants.public import AgentConfigRepository, SqlTenantDirectory

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class RetentionRun:
    """Итог по slug тенанта; `failed` — тенанты, чистка которых упала (подробности в логе)."""

    results: dict[str, PurgeResult] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)


async def purge_all_tenants(
    session_factory: Callable[[], AsyncSession],
    now: datetime,
    *,
    slugs: Sequence[str] = (),
    dry_run: bool = False,
) -> RetentionRun:
    """`slugs` пуст — все тенанты."""
    async with session_factory() as session:
        tenants = await SqlTenantDirectory(session).list_all()
    if slugs:
        tenants = [t for t in tenants if t.slug in slugs]
    run = RetentionRun()
    for tenant in tenants:
        try:
            run.results[tenant.slug] = await _purge_tenant(
                session_factory, tenant.id, now, dry_run=dry_run
            )
        except Exception:
            logger.exception("retention.failed", tenant_id=str(tenant.id), dry_run=dry_run)
            run.failed.append(tenant.slug)
    return run


async def _purge_tenant(
    session_factory: Callable[[], AsyncSession],
    tenant_id: TenantId,
    now: datetime,
    *,
    dry_run: bool,
) -> PurgeResult:
    async with session_factory() as session:
        active = await AgentConfigRepository(session).get_active(tenant_id)
        policy = retention_policy(tenant_id, active.config if active else None)
        conversations = ConversationRepository(session)
        agent_events = AgentEventRepository(session)
        leads = LeadRepository(session)
        if dry_run:
            result = await count_expired(
                tenant_id,
                policy,
                now,
                agent_events=agent_events.count_before,
                conversations=conversations.count_inactive,
                leads=leads.count_before,
            )
        else:
            result = await purge_expired(
                tenant_id,
                policy,
                now,
                agent_events=agent_events.purge_before,
                conversations=conversations.purge_inactive,
                leads=leads.purge_before,
                commit=session.commit,
            )
    logger.info(
        "retention.purged",
        tenant_id=str(tenant_id),
        dry_run=dry_run,
        conversations=result.conversations,
        agent_events=result.agent_events,
        leads=result.leads,
        conversations_days=policy.conversations_days,
        agent_events_days=policy.agent_events_days,
        leads_days=policy.leads_days,
    )
    return result
