"""Структурированные логи (structlog) и привязка trace_id к запросу."""

import logging
import re
import sys
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.settings import Settings

TRACE_ID_HEADER = "X-Trace-Id"

logger = structlog.get_logger(__name__)

# Трейсбек в JSON без локальных переменных фреймов: `dict_tracebacks` по умолчанию пишет их
# (`show_locals=True`), а там контакты из форм, токены, ключи API — мимо маскирования ADR-0032.
_exception_dict = structlog.processors.ExceptionRenderer(
    structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
)


# Логгеры библиотек со своими обработчиками и уровнем (uvicorn — из своего log_config, arq —
# из CLI, httpx — от Langfuse SDK при импорте): обработчики снимаются, уровень — от корневого
# (LOG_LEVEL), записи идут в корневой логгер и дальше через structlog (O-13). uvicorn
# `--no-access-log` (propagate=False) так отменился бы — не используется.
_OWN_HANDLER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "arq")
# httpx и httpcore на INFO пишут каждый запрос с полным URL (Langfuse, провайдеры LLM,
# источники знаний): в логах только их предупреждения, как было до O-13.
_QUIET_LOGGERS = ("httpx", "httpcore")


class _StdoutHandler(logging.Handler):
    """Пишет в текущий `sys.stdout` (как `PrintLogger` structlog), а не в поток, который был
    на момент настройки: так вывод перехватывается и в тестах (capsys)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            print(self.format(record), file=sys.stdout, flush=True)
        except Exception:
            self.handleError(record)


def configure_logging(settings: Settings) -> None:
    """Локально — читаемый вывод в консоль, в остальных окружениях — JSON.

    Логи stdlib `logging` (uvicorn, arq, httpx, Langfuse, OpenTelemetry) идут через те же
    процессоры: тот же формат и поля контекста — `tenant_id`, `trace_id`,
    `conversation_id` (REVIEW.md: O-13).
    """
    # ConsoleRenderer сам форматирует исключение; dict_tracebacks превратил бы трейсбек в список,
    # и logger.exception падал бы в локальном окружении.
    renderers: list[structlog.typing.Processor] = (
        [structlog.dev.ConsoleRenderer()]
        if settings.is_local
        else [_exception_dict, structlog.processors.JSONRenderer()]
    )
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    level = logging.getLevelNamesMapping()[settings.log_level]
    structlog.configure(
        processors=[*shared, *renderers],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        # В тестах приложение (и конфиг structlog) создаётся заново на каждый тест: закэшированный
        # логгер держал бы процессоры прежнего конфига, и перехват логов в тестах не сработал бы.
        cache_logger_on_first_use=settings.app_env != "test",
    )
    _route_stdlib_logging(shared, renderers, level)


def _route_stdlib_logging(
    shared: list[structlog.typing.Processor],
    renderers: list[structlog.typing.Processor],
    level: int,
) -> None:
    """Корневой логгер stdlib — одним обработчиком с `ProcessorFormatter` structlog.

    Повторный вызов (в тестах приложение создаётся на каждый тест) заменяет обработчик,
    а не добавляет второй; чужие обработчики корневого логгера (caplog pytest) не трогаются.
    """
    handler = _StdoutHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # Имя логгера — только у записей stdlib: отличить uvicorn.access от httpx.
            foreign_pre_chain=[*shared, structlog.stdlib.add_logger_name],
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderers],
        )
    )
    root = logging.getLogger()
    for old in [h for h in root.handlers if isinstance(h, _StdoutHandler)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    for name in (*_OWN_HANDLER_LOGGERS, *_QUIET_LOGGERS):
        library = logging.getLogger(name)
        library.handlers.clear()
        library.propagate = True
        library.setLevel(logging.NOTSET)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))


_TRACE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class TraceIdMiddleware:
    """Чистое ASGI-middleware: не ломает стриминг (SSE) и contextvars.

    Берёт trace_id из заголовка запроса или генерирует новый, кладёт в контекст
    structlog на время запроса и в `request.state.trace_id` (журнал хода, P7-02)
    и возвращает в заголовке ответа. Заголовок клиента недоверенный (REVIEW.md: O-6):
    принимается только `[A-Za-z0-9._-]{1,64}` — длинное значение не влезло бы в журнал
    (`String(128)`) и уронило бы запись хода, а мусор попал бы в логи и Langfuse.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = TRACE_ID_HEADER.lower().encode()
        incoming = dict(scope["headers"]).get(header)
        trace_id = incoming.decode("latin-1") if incoming else ""
        if not _TRACE_ID.fullmatch(trace_id):
            if trace_id:
                logger.debug("trace_id_rejected", length=len(trace_id))
            trace_id = uuid.uuid4().hex
        scope.setdefault("state", {})["trace_id"] = trace_id

        async def send_with_trace_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((header, trace_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        with structlog.contextvars.bound_contextvars(trace_id=trace_id):
            await self.app(scope, receive, send_with_trace_id)
