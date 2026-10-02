"""Сборка FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.logs import TraceIdMiddleware, configure_logging
from app.modules.shared.public import create_engine, create_session_factory
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
    app.add_middleware(TraceIdMiddleware)
    app.include_router(health.router)
    return app


app = create_app()
