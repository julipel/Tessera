"""Чистка по срокам хранения для всех тенантов (ADR-0043): задача воркера и `make retention`.

Сборка из публичных интерфейсов модулей (как `knowledge_wiring.py`): репозитории chat,
leads и observability, политика и порции — `chat.application.retention`.

Каждый тенант — своя сессия; сроки берутся из активного конфига. Сбой одного тенанта не
останавливает остальных: лог `retention.failed`, тенант попадает в `failed`. `dry_run`
только считает (`SELECT count(*)` с теми же условиями) — без транзакции удаления и блокировок.

Заодно, при чистке всех тенантов, удаляются истёкшие сессии админки (REVIEW.md: AC-10): они
не тенантские, и при входе чистятся только сессии того, кто входит.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.access.public import SqlAdminSessionStore
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
    """Итог по slug тенанта; `failed` — тенанты, чистка которых упала (подробности в логе).
    `admin_sessions` — истёкшие сессии админки; None — не чистились (выбраны тенанты) или
    чистка упала (тогда `admin_sessions_failed`)."""

    results: dict[str, PurgeResult] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    admin_sessions: int | None = None
    admin_sessions_failed: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.admin_sessions_failed


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
    if not slugs:
        try:
            run.admin_sessions = await _purge_admin_sessions(session_factory, now, dry_run=dry_run)
        except Exception:
            logger.exception("retention.admin_sessions_failed", dry_run=dry_run)
            run.admin_sessions_failed = True
    for tenant in tenants:
        try:
            run.results[tenant.slug] = await _purge_tenant(
                session_factory, tenant.id, now, dry_run=dry_run
            )
        except Exception:
            logger.exception("retention.failed", tenant_id=str(tenant.id), dry_run=dry_run)
            run.failed.append(tenant.slug)
    return run


async def _purge_admin_sessions(
    session_factory: Callable[[], AsyncSession], now: datetime, *, dry_run: bool
) -> int:
    async with session_factory() as session:
        sessions = SqlAdminSessionStore(session)
        if dry_run:
            return await sessions.count_expired(now)
        deleted = await sessions.purge_expired(now)
        await session.commit()
    logger.info("retention.admin_sessions_purged", deleted=deleted)
    return deleted


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
