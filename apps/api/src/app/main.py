"""Сборка FastAPI-приложения."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import structlog
from arq.connections import ArqRedis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import SecretStr
from redis.asyncio import Redis

from app.api import health
from app.knowledge_wiring import KnowledgeServices, build_knowledge_services
from app.logs import TraceIdMiddleware, configure_logging
from app.modules.access.public import KdfPool, SqlAdminAuthenticator
from app.modules.access.public import router as access_router
from app.modules.agent.public import LLMClients
from app.modules.chat.public import (
    RedisTurnDirectory,
    SummaryScheduler,
    TurnRegistry,
    background_summary,
)
from app.modules.chat.public import admin_router as chat_admin_router
from app.modules.chat.public import router as chat_router
from app.modules.knowledge.public import (
    ArqSyncQueue,
    LocalSourceFileStore,
    SourceFileLimits,
    SqlCatalog,
)
from app.modules.knowledge.public import admin_router as knowledge_admin_router
from app.modules.leads.public import SqlLeadStore
from app.modules.leads.public import admin_router as leads_admin_router
from app.modules.observability.public import Tracer, admin_router, build_tracer
from app.modules.shared.public import (
    BodySizeLimitMiddleware,
    RedisRateLimiter,
    create_engine,
    create_session_factory,
    install_error_handlers,
)
from app.modules.tenants.public import admin_router as tenants_admin_router
from app.modules.tenants.public import public_router as tenants_public_router
from app.settings import Settings

logger = structlog.get_logger(__name__)

# Сколько lifespan ждёт прерванные ходы при остановке (stop_grace_period в compose.prod.yml
# учитывает это время).
SHUTDOWN_TURNS_WAIT_S = 3.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Отмены ходов из других процессов API (ADR-0035).
    turns: TurnRegistry = app.state.turn_registry
    directory: RedisTurnDirectory = app.state.turn_directory
    cancel_listener = asyncio.create_task(directory.listen(turns.cancel_local))
    yield
    kdf: KdfPool = app.state.kdf_pool
    kdf.shutdown()  # первым: сбой закрытия соединений ниже не оставит потоки пула
    cancel_listener.cancel()
    with suppress(asyncio.CancelledError):
        await cancel_listener
    # Uvicorn по --timeout-graceful-shutdown отменяет ходы и сразу зовёт lifespan: даём
    # прерванным ходам дописать журнал и трейс до закрытия БД, Redis и трейсера (O-12).
    if not await turns.wait_idle(SHUTDOWN_TURNS_WAIT_S):
        logger.warning("shutdown.turns_pending")
    summaries: SummaryScheduler = app.state.summary_scheduler
    await summaries.aclose()
    knowledge: KnowledgeServices | None = app.state.knowledge
    if knowledge is not None:
        await knowledge.aclose()
    tracer: Tracer = app.state.tracer
    await asyncio.to_thread(tracer.shutdown)  # отправка накопленных трейсов — блокирующая
    redis: Redis = app.state.redis
    await redis.aclose()
    subscriber: Redis = app.state.redis_subscriber
    await subscriber.aclose()
    jobs: ArqRedis = app.state.arq_redis
    await jobs.aclose()
    await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)

    app = FastAPI(title="AI-консультант API", lifespan=lifespan)
    app.state.settings = settings
    # Engine ленивый (соединения открываются при первом запросе), поэтому создаём сразу,
    # а не в lifespan: так он доступен и в тестах без запуска lifespan.
    app.state.engine = create_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        statement_timeout_s=settings.db_statement_timeout_s,
    )
    app.state.session_factory = create_session_factory(app.state.engine)
    # Redis — для лимитов частоты (ADR-0030); соединение ленивое, таймауты короткие:
    # недоступный Redis не должен держать запрос.
    app.state.redis = Redis.from_url(
        settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5
    )
    app.state.rate_limiter = RedisRateLimiter(app.state.redis)
    # Подписка на отмены ходов ждёт сообщений без таймаута чтения — свой клиент.
    app.state.redis_subscriber = Redis.from_url(
        settings.redis_url, socket_connect_timeout=0.5, health_check_interval=30
    )
    # Текущие ходы — для отмены из любого процесса API (ADR-0035).
    app.state.turn_directory = RedisTurnDirectory(app.state.redis, app.state.redis_subscriber)
    app.state.turn_registry = TurnRegistry(app.state.turn_directory)
    app.state.llm_clients = LLMClients(
        openai_api_key=_secret(settings.openai_api_key),
        openai_base_url=settings.openai_base_url,
        openai_compatible_api_key=_secret(settings.openai_compatible_api_key),
        openai_compatible_base_url=settings.openai_compatible_base_url,
        anthropic_api_key=_secret(settings.anthropic_api_key),
        anthropic_base_url=settings.anthropic_base_url,
    )
    # Трейсы ходов в Langfuse (ADR-0029); без ключей — выключены.
    app.state.tracer = build_tracer(
        settings.langfuse_public_key,
        _secret(settings.langfuse_secret_key),
        settings.langfuse_host,
        settings.app_env,
        settings.langfuse_timeout_s,
    )
    # Сводка длинной истории после хода — фоновой задачей процесса (ADR-0024).
    app.state.summary_scheduler = SummaryScheduler(
        background_summary(app.state.session_factory, app.state.llm_clients.for_provider)
    )
    # Поиск по знаниям для search_knowledge; None без OPENAI_API_KEY.
    app.state.knowledge = build_knowledge_services(settings)
    app.state.knowledge_search = app.state.knowledge.search if app.state.knowledge else None
    # Каталог для search_catalog/get_entity: своя сессия на вызов инструмента (ADR-0016).
    app.state.catalog = SqlCatalog(app.state.session_factory)
    # Заявки create_lead: своя сессия и commit на заявку.
    app.state.leads = SqlLeadStore(app.state.session_factory)
    # Синхронизации источников из админки — в очередь воркера (arq, тот же Redis).
    app.state.arq_redis = ArqRedis.from_url(
        settings.redis_url, socket_timeout=2, socket_connect_timeout=2
    )
    app.state.sync_queue = ArqSyncQueue(app.state.arq_redis)
    # Файлы источников file/table из админки — в каталог, который читают коннекторы воркера.
    app.state.source_files = LocalSourceFileStore(settings.knowledge_files_dir)
    app.state.source_file_limits = SourceFileLimits()
    # Сессии админки для зависимостей ролей в любом модуле (ADR-0036).
    app.state.admin_authenticator = SqlAdminAuthenticator(app.state.session_factory)
    app.state.kdf_pool = KdfPool(settings.admin_kdf_threads)
    install_error_handlers(app)
    # Размер тела — до чтения и разбора JSON (REVIEW.md: C-3): чат анонимный — 64 КБ (ход
    # проверяется ещё и на 32 КБ после разбора), файлы источников читаются потоково со своим
    # лимитом (413), остальное — до 1 МБ (YAML конфига агента).
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_bytes=1024 * 1024,
        rules=[
            (r"/v1/conversations(/|$)", 64 * 1024),
            (r"/v1/admin/tenants/[^/]+/sources/[^/]+/files/", None),
        ],
    )
    app.add_middleware(TraceIdMiddleware)  # после: снаружи обработчика 500
    # Последним — снаружи всех: preflight отвечается до остальной обработки.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # Cookie сессии админки (ADR-0044): origin перечислены явно, «*» с credentials нельзя.
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Widget-Key"],
        expose_headers=["X-Trace-Id", "Retry-After"],
    )
    app.include_router(health.router)
    app.include_router(tenants_public_router)
    app.include_router(chat_router)
    app.include_router(access_router)
    app.include_router(admin_router)
    app.include_router(tenants_admin_router)
    app.include_router(knowledge_admin_router)
    app.include_router(chat_admin_router)
    app.include_router(leads_admin_router)
    return app


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value else None


app = create_app()
