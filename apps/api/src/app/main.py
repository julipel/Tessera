"""Сборка FastAPI-приложения."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import SecretStr
from redis.asyncio import Redis

from app.api import health
from app.knowledge_wiring import KnowledgeServices, build_knowledge_services
from app.logs import TraceIdMiddleware, configure_logging
from app.modules.agent.public import LLMClients
from app.modules.chat.public import (
    RedisTurnDirectory,
    SummaryScheduler,
    TurnRegistry,
    background_summary,
)
from app.modules.chat.public import router as chat_router
from app.modules.knowledge.public import SqlCatalog
from app.modules.leads.public import SqlLeadStore
from app.modules.observability.public import Tracer, admin_router, build_tracer
from app.modules.shared.public import (
    RedisRateLimiter,
    create_engine,
    create_session_factory,
    install_error_handlers,
)
from app.modules.tenants.public import public_router as tenants_public_router
from app.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Отмены ходов из других процессов API (ADR-0035).
    turns: TurnRegistry = app.state.turn_registry
    directory: RedisTurnDirectory = app.state.turn_directory
    cancel_listener = asyncio.create_task(directory.listen(turns.cancel_local))
    yield
    cancel_listener.cancel()
    with suppress(asyncio.CancelledError):
        await cancel_listener
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
    install_error_handlers(app)
    app.add_middleware(TraceIdMiddleware)  # после: снаружи обработчика 500
    # Последним — снаружи всех: preflight отвечается до остальной обработки.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Widget-Key"],
        expose_headers=["X-Trace-Id", "Retry-After"],
    )
    app.include_router(health.router)
    app.include_router(tenants_public_router)
    app.include_router(chat_router)
    app.include_router(admin_router)
    return app


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value else None


app = create_app()
