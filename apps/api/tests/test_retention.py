"""Сроки хранения данных посетителей (ADR-0043): политика, порции, удаление по тенанту."""

import itertools
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app import cli, worker
from app.modules.access.public import AdminSessionRecord, AdminUserRecord
from app.modules.chat.public import (
    ConversationRecord,
    MessageRatingRecord,
    MessageRecord,
    PurgeResult,
    RetentionConfigInvalidError,
    RetentionPolicy,
    ToolCallRecord,
    purge_expired,
    retention_policy,
)
from app.modules.leads.public import LeadRecord
from app.modules.observability.public import AgentEventRecord
from app.modules.shared.public import TenantId
from app.modules.tenants.public import AgentConfigRepository, SqlTenantDirectory
from app.retention import RetentionRun, purge_all_tenants
from app.worker import RETENTION_TIMEOUT_S, WorkerSettings, purge_expired_data

NOW = datetime(2026, 10, 16, 3, 17, tzinfo=UTC)
TENANT = TenantId(uuid4())
# Номер заявки уникален в тенанте (ADR-0045); тестам нужен только уникальный.
_LEAD_NUMBERS = itertools.count(1)


def days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


# --- политика из конфига ---


def test_policy_defaults_without_config_or_section() -> None:
    assert retention_policy(TENANT, None) == RetentionPolicy(180, 90, 365)
    assert retention_policy(TENANT, {"assistant": {}}) == RetentionPolicy(180, 90, 365)


def test_policy_from_config_with_partial_section() -> None:
    policy = retention_policy(TENANT, {"retention": {"conversations_days": 30}})

    assert policy == RetentionPolicy(conversations_days=30, agent_events_days=90, leads_days=365)


def test_invalid_section_is_an_error_not_defaults() -> None:
    """Удаление необратимо: умолчания могли бы оказаться короче сроков тенанта."""
    with pytest.raises(RetentionConfigInvalidError):
        retention_policy(TENANT, {"retention": {"conversations_days": 0}})


def test_agent_events_live_no_longer_than_conversations() -> None:
    policy = RetentionPolicy(conversations_days=30, agent_events_days=90)

    assert policy.agent_events_before(NOW) == days_ago(30)
    assert policy.conversations_before(NOW) == days_ago(30)
    assert policy.leads_before(NOW) == days_ago(365)


# --- порции ---


class FakePurger:
    def __init__(self, name: str, rows: int, log: list[str]) -> None:
        self.name = name
        self.rows = rows
        self.log = log
        self.befores: list[datetime] = []
        self.limits: list[int] = []

    async def __call__(self, tenant_id: TenantId, before: datetime, limit: int) -> int:
        assert tenant_id == TENANT
        self.befores.append(before)
        self.limits.append(limit)
        deleted = min(self.rows, limit)
        self.rows -= deleted
        self.log.append(f"{self.name}:{deleted}")
        return deleted


async def test_purge_drains_in_batches_with_commit_after_each() -> None:
    log: list[str] = []
    events, conversations, leads = (
        FakePurger("events", 5, log),
        FakePurger("conversations", 2, log),
        FakePurger("leads", 0, log),
    )

    async def commit() -> None:
        log.append("commit")

    result = await purge_expired(
        TENANT,
        RetentionPolicy(conversations_days=30, agent_events_days=10, leads_days=60),
        NOW,
        agent_events=events,
        conversations=conversations,
        leads=leads,
        commit=commit,
        batch=2,
        conversation_batch=1,
    )

    assert result == PurgeResult(conversations=2, agent_events=5, leads=0)
    # Порция меньше batch — источник исчерпан; полная порция — запрос ещё одной.
    assert log == [
        "events:2", "commit", "events:2", "commit", "events:1", "commit",
        "conversations:1", "commit", "conversations:1", "commit", "conversations:0", "commit",
        "leads:0", "commit",
    ]  # fmt: skip
    assert set(events.befores) == {days_ago(10)}
    assert set(conversations.befores) == {days_ago(30)}
    assert set(leads.befores) == {days_ago(60)}
    # Порция диалогов своя, меньшая: каскад тянет их сообщения и журнал.
    assert events.limits == [2, 2, 2]
    assert conversations.limits == [1, 1, 1]


# --- удаление в БД ---

POLICY = {"conversations_days": 30, "agent_events_days": 10, "leads_days": 60}


async def tenant_with_policy(session: AsyncSession, slug: str) -> tuple[TenantId, UUID]:
    tenant = await SqlTenantDirectory(session).create(slug, slug)
    configs = AgentConfigRepository(session)
    config = await configs.create_draft(tenant.id, {"assistant": {}, "retention": POLICY})
    await configs.activate(tenant.id, config.id)
    return tenant.id, config.id


async def conversation(
    session: AsyncSession,
    tenant_id: TenantId,
    config_id: UUID,
    *,
    created: datetime,
    messages: tuple[datetime, ...] = (),
    events: tuple[datetime, ...] = (),
    leads: tuple[datetime, ...] = (),
) -> UUID:
    record = ConversationRecord(
        tenant_id=tenant_id,
        agent_config_id=config_id,
        channel="web",
        visitor_id="v",
        created_at=created,
    )
    session.add(record)
    await session.flush()
    for at in messages:
        message = MessageRecord(
            tenant_id=tenant_id,
            conversation_id=record.id,
            role="assistant",
            status="completed",
            content="Ваш телефон +7 900 000-00-00?",
            created_at=at,
        )
        session.add(message)
        await session.flush()
        session.add_all(
            [
                ToolCallRecord(
                    tenant_id=tenant_id,
                    message_id=message.id,
                    tool_call_id="call_1",
                    name="create_lead",
                    arguments={"phone": "+79000000000"},
                    duration_ms=1,
                    created_at=at,
                ),
                MessageRatingRecord(
                    tenant_id=tenant_id,
                    conversation_id=record.id,
                    message_id=message.id,
                    rating="good",
                ),
            ]
        )
    session.add_all(
        AgentEventRecord(
            tenant_id=tenant_id,
            conversation_id=record.id,
            turn_id=uuid4(),
            trace_id="t",
            seq=seq,
            type="turn_completed",
            payload={},
            ts=at,
        )
        for seq, at in enumerate(events)
    )
    session.add_all(
        LeadRecord(
            tenant_id=tenant_id,
            conversation_id=record.id,
            number=next(_LEAD_NUMBERS),
            form_key="contact",
            fields={"phone": "+79000000000"},
            created_at=at,
        )
        for at in leads
    )
    await session.flush()
    return record.id


async def count(session: AsyncSession, model: Any, tenant_id: TenantId) -> int:
    stmt = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    return int((await session.execute(stmt)).scalar_one())


def factory(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


async def seed_tenant(session: AsyncSession, slug: str) -> dict[str, Any]:
    tenant_id, config_id = await tenant_with_policy(session, slug)
    expired = await conversation(
        session,
        tenant_id,
        config_id,
        created=days_ago(40),
        messages=(days_ago(40), days_ago(35)),
        events=(days_ago(35),),
        leads=(days_ago(35),),  # моложе 60 дней — переживает диалог
    )
    # Создан давно, но писали недавно: диалог живёт, старый журнал — нет.
    active = await conversation(
        session,
        tenant_id,
        config_id,
        created=days_ago(100),
        messages=(days_ago(100), days_ago(5)),
        events=(days_ago(15), days_ago(5)),
        leads=(days_ago(70),),  # старше 60 дней — удаляется, хоть диалог и жив
    )
    # Без сообщений (создан и брошен): срок — от создания.
    fresh = await conversation(session, tenant_id, config_id, created=days_ago(3))
    await conversation(session, tenant_id, config_id, created=days_ago(40))  # удаляется
    # Граница: сообщение ровно на сроке ещё не просрочено, создание на сроке — тоже.
    edge = await conversation(
        session, tenant_id, config_id, created=days_ago(50), messages=(days_ago(30),)
    )
    edge_created = await conversation(session, tenant_id, config_id, created=days_ago(30))
    await session.commit()
    return {
        "tenant_id": tenant_id,
        "kept": {active, fresh, edge, edge_created},
        "expired": expired,
    }


async def test_purge_deletes_expired_data_and_keeps_the_rest(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    shop = await seed_tenant(db_session, "shop")
    other = await seed_tenant(db_session, "other")
    shop_id = shop["tenant_id"]

    run = await purge_all_tenants(factory(db_connection), NOW, slugs=["shop"])

    assert run.results == {"shop": PurgeResult(conversations=2, agent_events=2, leads=1)}
    assert run.failed == []
    conversations = (
        await db_session.execute(
            select(ConversationRecord.id).where(ConversationRecord.tenant_id == shop_id)
        )
    ).scalars()
    assert set(conversations) == shop["kept"]
    # Каскад от удалённого диалога: его сообщения, вызовы, оценки и журнал.
    assert await count(db_session, MessageRecord, shop_id) == 3
    assert await count(db_session, ToolCallRecord, shop_id) == 3
    assert await count(db_session, MessageRatingRecord, shop_id) == 3
    events = (
        await db_session.execute(
            select(AgentEventRecord.ts).where(AgentEventRecord.tenant_id == shop_id)
        )
    ).scalars()
    assert list(events) == [days_ago(5)]
    # Заявка удалённого диалога осталась без ссылки на него; старая заявка удалена.
    leads = (
        await db_session.execute(
            select(LeadRecord.conversation_id, LeadRecord.created_at).where(
                LeadRecord.tenant_id == shop_id
            )
        )
    ).all()
    assert leads == [(None, days_ago(35))]
    # Другой тенант не затронут.
    other_id = other["tenant_id"]
    assert await count(db_session, ConversationRecord, other_id) == 6
    assert await count(db_session, AgentEventRecord, other_id) == 3
    assert await count(db_session, LeadRecord, other_id) == 2


async def test_dry_run_counts_without_deleting(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    shop = await seed_tenant(db_session, "shop")

    run = await purge_all_tenants(factory(db_connection), NOW, slugs=["shop"], dry_run=True)

    # Те же числа, что удалила бы настоящая чистка (см. тест выше), — только чтением.
    assert run.results == {"shop": PurgeResult(conversations=2, agent_events=2, leads=1)}
    assert await count(db_session, ConversationRecord, shop["tenant_id"]) == 6
    assert await count(db_session, AgentEventRecord, shop["tenant_id"]) == 3
    assert await count(db_session, LeadRecord, shop["tenant_id"]) == 2


async def test_tenant_without_active_config_uses_defaults(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    tenant = await SqlTenantDirectory(db_session).create("bare", "bare")
    draft = await AgentConfigRepository(db_session).create_draft(tenant.id, {"assistant": {}})
    kept = await conversation(
        db_session, tenant.id, draft.id, created=days_ago(100), messages=(days_ago(100),)
    )
    await conversation(
        db_session, tenant.id, draft.id, created=days_ago(200), messages=(days_ago(200),)
    )
    await db_session.commit()

    run = await purge_all_tenants(factory(db_connection), NOW, slugs=["bare"])

    assert run.results["bare"].conversations == 1
    remaining = await db_session.execute(
        select(ConversationRecord.id).where(ConversationRecord.tenant_id == tenant.id)
    )
    assert list(remaining.scalars()) == [kept]


async def test_tenant_failure_does_not_stop_others(
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    debug_logs: list[MutableMapping[str, Any]],
) -> None:
    """Неисправная секция retention у тенанта «aaa» (первого по slug): он пропущен с ошибкой,
    следующий почищен."""
    broken = await SqlTenantDirectory(db_session).create("aaa", "aaa")
    configs = AgentConfigRepository(db_session)
    config = await configs.create_draft(
        broken.id, {"assistant": {}, "retention": {"conversations_days": 0}}
    )
    await configs.activate(broken.id, config.id)
    await conversation(db_session, broken.id, config.id, created=days_ago(400))
    shop = await seed_tenant(db_session, "shop")

    run = await purge_all_tenants(factory(db_connection), NOW, slugs=["aaa", "shop"])

    assert run.failed == ["aaa"]
    assert run.results["shop"].conversations == 2
    # Данные тенанта с неисправным конфигом не тронуты умолчаниями.
    assert await count(db_session, ConversationRecord, broken.id) == 1
    assert await count(db_session, ConversationRecord, shop["tenant_id"]) == 4
    (failed,) = [log for log in debug_logs if log["event"] == "retention.failed"]
    assert failed["tenant_id"] == str(broken.id)


async def test_full_run_purges_expired_admin_sessions(
    db_connection: AsyncConnection, db_session: AsyncSession
) -> None:
    """AC-10: истёкшие сессии админки всех пользователей — в ежедневной чистке; при выборе
    тенантов (`slugs`) не трогаются."""
    user = AdminUserRecord(email="anna@example.com", password_hash="x", is_superadmin=False)
    db_session.add(user)
    await db_session.flush()
    for name, expires in (("old", NOW - timedelta(hours=1)), ("live", NOW + timedelta(hours=1))):
        db_session.add(AdminSessionRecord(user_id=user.id, token_hash=name, expires_at=expires))
    await db_session.commit()

    assert (
        await purge_all_tenants(factory(db_connection), NOW, slugs=["x"])
    ).admin_sessions is None
    dry = await purge_all_tenants(factory(db_connection), NOW, dry_run=True)
    assert dry.admin_sessions == 1
    run = await purge_all_tenants(factory(db_connection), NOW)

    assert run.admin_sessions == 1 and run.ok
    remaining = await db_session.execute(select(AdminSessionRecord.token_hash))
    assert list(remaining.scalars()) == ["live"]


async def test_admin_sessions_failure_does_not_stop_tenants(
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_: Any, **__: Any) -> int:
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr("app.retention._purge_admin_sessions", broken)
    shop = await seed_tenant(db_session, "shop")

    run = await purge_all_tenants(factory(db_connection), NOW)

    assert run.admin_sessions_failed and not run.ok
    assert run.results["shop"].conversations == 2
    assert await count(db_session, ConversationRecord, shop["tenant_id"]) == 4


def test_cli_reports_admin_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run(*_: Any, **__: Any) -> RetentionRun:
        return RetentionRun(admin_sessions=3)

    monkeypatch.setattr(cli, "purge_all_tenants", fake_run)
    assert cli.main(["retention"]) == 0
    assert capsys.readouterr().out == "истёкших сессий админки удалено: 3\n"

    async def failed_run(*_: Any, **__: Any) -> RetentionRun:
        return RetentionRun(admin_sessions_failed=True)

    monkeypatch.setattr(cli, "purge_all_tenants", failed_run)
    assert cli.main(["retention"]) == 1
    assert "сессии админки: сбой чистки" in capsys.readouterr().err


async def test_worker_job_fails_when_admin_sessions_purge_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_with_failure(*_: Any, **__: Any) -> RetentionRun:
        return RetentionRun(admin_sessions_failed=True)

    monkeypatch.setattr(worker, "purge_all_tenants", run_with_failure)

    with pytest.raises(RuntimeError, match="сессии админки"):
        await purge_expired_data({"session_factory": None, "job_id": "cron:retention"})


async def test_worker_job_fails_when_a_tenant_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_with_failure(*_: Any, **__: Any) -> RetentionRun:
        return RetentionRun(results={"shop": PurgeResult()}, failed=["aaa"])

    monkeypatch.setattr(worker, "purge_all_tenants", run_with_failure)

    with pytest.raises(RuntimeError, match="aaa"):
        await purge_expired_data({"session_factory": None, "job_id": "cron:retention"})


def test_cli_reports_unknown_tenant_and_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run(*_: Any, **kwargs: Any) -> RetentionRun:
        assert kwargs == {"slugs": ["shop", "aaa", "ghost"], "dry_run": True}
        return RetentionRun(results={"shop": PurgeResult(2, 1, 0)}, failed=["aaa"])

    monkeypatch.setattr(cli, "purge_all_tenants", fake_run)

    code = cli.main(["retention", "shop", "aaa", "ghost", "--dry-run"])

    out, err = capsys.readouterr()
    assert code == 1
    assert out == "shop: удалится диалогов 2, событий журнала 1, заявок 0\n"
    assert "aaa: сбой чистки" in err
    assert "ghost: тенант не найден" in err


def test_worker_runs_purge_daily_once_across_workers() -> None:
    (job,) = WorkerSettings.cron_jobs
    assert job.coroutine is purge_expired_data
    assert (job.hour, job.minute, job.unique) == (3, 17, True)
    assert job.timeout_s == RETENTION_TIMEOUT_S
