"""Структурированные логи (structlog) и привязка trace_id к запросу."""

import logging
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.settings import Settings

TRACE_ID_HEADER = "X-Trace-Id"


def configure_logging(settings: Settings) -> None:
    """Локально — читаемый вывод в консоль, в остальных окружениях — JSON."""
    # ConsoleRenderer сам форматирует исключение; dict_tracebacks превратил бы трейсбек в список,
    # и logger.exception падал бы в локальном окружении.
    renderers: list[structlog.typing.Processor] = (
        [structlog.dev.ConsoleRenderer()]
        if settings.is_local
        else [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            *renderers,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        # В тестах приложение (и конфиг structlog) создаётся заново на каждый тест: закэшированный
        # логгер держал бы процессоры прежнего конфига, и перехват логов в тестах не сработал бы.
        cache_logger_on_first_use=settings.app_env != "test",
    )


class TraceIdMiddleware:
    """Чистое ASGI-middleware: не ломает стриминг (SSE) и contextvars.

    Берёт trace_id из заголовка запроса или генерирует новый, кладёт в контекст
    structlog на время запроса и возвращает в заголовке ответа.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = TRACE_ID_HEADER.lower().encode()
        incoming = dict(scope["headers"]).get(header)
        trace_id = incoming.decode("latin-1") if incoming else uuid.uuid4().hex

        async def send_with_trace_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((header, trace_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        with structlog.contextvars.bound_contextvars(trace_id=trace_id):
            await self.app(scope, receive, send_with_trace_id)
