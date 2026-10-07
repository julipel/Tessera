"""Единый формат HTTP-ошибок публичного API: `{"error": {code, message, retryable}}`.

Контракт — `http_error.schema.json`, коды — docs/contracts.md §6.
"""

from collections.abc import Mapping
from typing import Literal, get_args

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.contracts import HttpError, HttpErrorBody

# Дублирует enum из схемы ради типизации вызовов; совпадение проверяет тест.
type ErrorCode = Literal[
    "llm_unavailable",
    "turn_timeout",
    "step_limit",
    "invalid_input",
    "conversation_not_found",
    "rate_limited",
    "internal",
    "unauthorized",
    "forbidden",
    "not_found",
    "duplicate_message",
    "not_retryable",
]
ERROR_CODES: tuple[str, ...] = get_args(ErrorCode.__value__)

RETRYABLE: frozenset[ErrorCode] = frozenset({"llm_unavailable", "turn_timeout", "rate_limited"})

# Для HTTPException фреймворка (неизвестный маршрут, неверный метод и т.п.).
_CODE_BY_STATUS: dict[int, ErrorCode] = {
    status.HTTP_400_BAD_REQUEST: "invalid_input",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "invalid_input",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "invalid_input",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
}

logger = structlog.get_logger(__name__)


class ApiError(Exception):
    """Ошибка, которую роутер отдаёт клиенту как есть: статус, код и сообщение."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = HttpError(error=HttpErrorBody(code=code, message=message, retryable=code in RETRYABLE))
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code, headers=headers)


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status_code, exc.code, exc.message, exc.headers)


async def _http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _CODE_BY_STATUS.get(exc.status_code, "internal")
    return error_response(exc.status_code, code, str(exc.detail), headers=exc.headers)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    problems = [
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
    ]
    return error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input", "; ".join(problems)
    )


class UnhandledErrorMiddleware:
    """Необработанное исключение → 500 `internal` в едином формате.

    Ставится внутри TraceIdMiddleware: лог ошибки получает trace_id, ответ — X-Trace-Id.
    Если ответ уже начат (стрим), исключение пробрасывается дальше — тело не заменить.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_tracking(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking)
        except Exception:
            logger.exception("unhandled_error", path=scope["path"])
            if started:
                raise
            response = error_response(
                status.HTTP_500_INTERNAL_SERVER_ERROR, "internal", "внутренняя ошибка сервера"
            )
            await response(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    """Обработчики ошибок; middleware добавить до TraceIdMiddleware (оно должно быть снаружи)."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_middleware(UnhandledErrorMiddleware)
