"""Доступ к API админки по токену из env — до пользователей и ролей админки (P8-01, ADR-0028)."""

import hmac
from typing import Annotated

from fastapi import Depends, Header, Request, status
from pydantic import SecretStr

from app.modules.shared.api.errors import ApiError


async def require_admin(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    """404, если токен админки не задан (эндпоинтов как будто нет); 401 — нет заголовка
    `Authorization: Bearer <ADMIN_API_TOKEN>` или токен не тот."""
    token: SecretStr | None = request.app.state.settings.admin_api_token
    if token is None or not token.get_secret_value():
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "API админки выключено")
    scheme, _, given = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        given.strip().encode(), token.get_secret_value().encode()
    ):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "unauthorized", "неверный токен админки")


AdminAccess = Depends(require_admin)
