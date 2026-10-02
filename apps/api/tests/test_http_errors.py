"""Единый формат HTTP-ошибок (http_error.schema.json) вместо дефолтного `{"detail"}`."""

from collections.abc import AsyncIterator
from typing import Any, get_args

import pytest
import structlog
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient, Response
from structlog.testing import capture_logs

from app.contracts import HttpError, HttpErrorBody
from app.logs import TRACE_ID_HEADER
from app.modules.shared.api.errors import ERROR_CODES
from app.modules.shared.public import ApiError


async def _boom() -> None:
    raise RuntimeError("секрет из стектрейса")


async def _rate_limited() -> None:
    raise ApiError(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", "слишком часто")


async def _typed(n: int) -> dict[str, int]:
    return {"n": n}


@pytest.fixture
async def errors_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    app.add_api_route("/test/boom", _boom)
    app.add_api_route("/test/rate-limited", _rate_limited)
    app.add_api_route("/test/typed", _typed)
    # raise_app_exceptions=False: проверяем ответ клиенту, а не проброс исключения в тест.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _error(response: Response) -> HttpErrorBody:
    return HttpError.model_validate(response.json()).error


def test_error_codes_match_contract() -> None:
    annotation: Any = HttpErrorBody.model_fields["code"].annotation
    assert set(ERROR_CODES) == set(get_args(annotation))


async def test_api_error_keeps_status_code_and_retryable(errors_client: AsyncClient) -> None:
    response = await errors_client.get("/test/rate-limited")

    assert response.status_code == 429
    error = _error(response)
    assert (error.code, error.message, error.retryable) == ("rate_limited", "слишком часто", True)


async def test_validation_error_is_invalid_input(errors_client: AsyncClient) -> None:
    response = await errors_client.get("/test/typed", params={"n": "abc"})

    assert response.status_code == 422
    error = _error(response)
    assert error.code == "invalid_input"
    assert error.retryable is False
    assert "query.n" in error.message


async def test_unknown_route_is_not_found(errors_client: AsyncClient) -> None:
    response = await errors_client.get("/nope")

    assert response.status_code == 404
    assert _error(response).code == "not_found"


async def test_wrong_method_is_invalid_input(errors_client: AsyncClient) -> None:
    response = await errors_client.post("/health")

    assert response.status_code == 405
    assert _error(response).code == "invalid_input"


async def test_unhandled_exception_is_internal_with_trace_id(errors_client: AsyncClient) -> None:
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        response = await errors_client.get("/test/boom", headers={TRACE_ID_HEADER: "trace-500"})

    assert response.status_code == 500
    error = _error(response)
    assert (error.code, error.retryable) == ("internal", False)
    assert "секрет" not in response.text
    assert response.headers[TRACE_ID_HEADER] == "trace-500"
    [entry] = [e for e in logs if e["event"] == "unhandled_error"]
    assert entry["trace_id"] == "trace-500"
    assert entry["log_level"] == "error"
