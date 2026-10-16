"""Чистка данных посетителей по срокам хранения тенанта (ADR-0043).

Порядок: журнал хода, диалоги (с каскадом сообщений, вызовов, оценок и остатка журнала),
заявки. Каждая порция — свой commit: транзакции короткие, блокировки не копятся, прерванная
чистка продолжится со следующего запуска. Порция диалогов меньше: каскад тянет все их
сообщения, вызовы и журнал.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

import structlog
from pydantic import ValidationError

from app.contracts import RetentionConfig
from app.modules.chat.domain.retention import (
    Counter,
    Purger,
    PurgeResult,
    RetentionConfigInvalidError,
    RetentionPolicy,
)
from app.modules.shared.kernel import TenantId

logger = structlog.get_logger(__name__)

DEFAULT_BATCH = 500
CONVERSATION_BATCH = 100


def retention_policy(tenant_id: TenantId, config: Mapping[str, Any] | None) -> RetentionPolicy:
    """Сроки из активного конфига; нет конфига или секции — умолчания. Секция не проходит
    схему — RetentionConfigInvalidError: удаление необратимо, а умолчания могут оказаться
    короче сроков, которые задал тенант."""
    section = (config or {}).get("retention") or {}
    try:
        parsed = RetentionConfig.model_validate(section)
    except ValidationError as e:
        raise RetentionConfigInvalidError(f"retention тенанта {tenant_id} не проходит схему") from e
    defaults = RetentionPolicy()
    return RetentionPolicy(
        conversations_days=parsed.conversations_days or defaults.conversations_days,
        agent_events_days=parsed.agent_events_days or defaults.agent_events_days,
        leads_days=parsed.leads_days or defaults.leads_days,
    )


async def purge_expired(
    tenant_id: TenantId,
    policy: RetentionPolicy,
    now: datetime,
    *,
    agent_events: Purger,
    conversations: Purger,
    leads: Purger,
    commit: Callable[[], Awaitable[None]],
    batch: int = DEFAULT_BATCH,
    conversation_batch: int = CONVERSATION_BATCH,
) -> PurgeResult:
    async def drain(purge: Purger, before: datetime, limit: int) -> int:
        total = 0
        while True:
            deleted = await purge(tenant_id, before, limit)
            await commit()
            total += deleted
            # Неполная порция — строк больше нет (или их удалил параллельный запуск, или
            # они заняты другой транзакцией): остаток уйдёт в следующий запуск.
            if deleted < limit:
                return total

    return PurgeResult(
        agent_events=await drain(agent_events, policy.agent_events_before(now), batch),
        conversations=await drain(
            conversations, policy.conversations_before(now), conversation_batch
        ),
        leads=await drain(leads, policy.leads_before(now), batch),
    )


async def count_expired(
    tenant_id: TenantId,
    policy: RetentionPolicy,
    now: datetime,
    *,
    agent_events: Counter,
    conversations: Counter,
    leads: Counter,
) -> PurgeResult:
    """То же, что удалила бы `purge_expired`, — только чтением: журнал считается первым,
    как и удаляется, заявки удалённых диалогов не удаляются (SET NULL), так что числа
    совпадают."""
    return PurgeResult(
        agent_events=await agent_events(tenant_id, policy.agent_events_before(now)),
        conversations=await conversations(tenant_id, policy.conversations_before(now)),
        leads=await leads(tenant_id, policy.leads_before(now)),
    )
