"""Сборка FastAPI-приложения."""

from fastapi import FastAPI

from app.api import health
from app.logs import TraceIdMiddleware, configure_logging
from app.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)

    app = FastAPI(title="AI-консультант API")
    app.state.settings = settings
    app.add_middleware(TraceIdMiddleware)
    app.include_router(health.router)
    return app


app = create_app()
