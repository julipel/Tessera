"""Сборка FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import SecretStr

from app.api import health
from app.knowledge_wiring import KnowledgeServices, build_knowledge_services
from app.logs import TraceIdMiddleware, configure_logging
from app.modules.agent.public import LLMClients
from app.modules.chat.public import TurnRegistry
from app.modules.chat.public import router as chat_router
from app.modules.knowledge.public import SqlCatalog
from app.modules.shared.public import (
    create_engine,
    create_session_factory,
    install_error_handlers,
)
from app.modules.tenants.public import public_router as tenants_public_router
from app.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    knowledge: KnowledgeServices | None = app.state.knowledge
    if knowledge is not None:
        await knowledge.aclose()
    await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)

    app = FastAPI(title="AI-консультант API", lifespan=lifespan)
    app.state.settings = settings
    # Engine ленивый (соединения открываются при первом запросе), поэтому создаём сразу,
    # а не в lifespan: так он доступен и в тестах без запуска lifespan.
    app.state.engine = create_engine(settings.database_url, echo=settings.database_echo)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.turn_registry = TurnRegistry()  # текущие ходы процесса — для отмены
    app.state.llm_clients = LLMClients(
        openai_api_key=_secret(settings.openai_api_key),
        openai_base_url=settings.openai_base_url,
        openai_compatible_api_key=_secret(settings.openai_compatible_api_key),
        openai_compatible_base_url=settings.openai_compatible_base_url,
        anthropic_api_key=_secret(settings.anthropic_api_key),
        anthropic_base_url=settings.anthropic_base_url,
    )
    # Поиск по знаниям для search_knowledge; None без OPENAI_API_KEY.
    app.state.knowledge = build_knowledge_services(settings)
    app.state.knowledge_search = app.state.knowledge.search if app.state.knowledge else None
    # Каталог для search_catalog/get_entity: своя сессия на вызов инструмента (ADR-0016).
    app.state.catalog = SqlCatalog(app.state.session_factory)
    install_error_handlers(app)
    app.add_middleware(TraceIdMiddleware)  # после: снаружи обработчика 500
    # Последним — снаружи всех: preflight отвечается до остальной обработки.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Widget-Key"],
        expose_headers=["X-Trace-Id"],
    )
    app.include_router(health.router)
    app.include_router(tenants_public_router)
    app.include_router(chat_router)
    return app


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value else None


app = create_app()
