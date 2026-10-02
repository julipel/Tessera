"""Сборка FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.logs import TraceIdMiddleware, configure_logging
from app.modules.chat.public import TurnRegistry
from app.modules.chat.public import router as chat_router
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


app = create_app()
