"""FastAPI-зависимости для работы с БД."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Сессия на запрос: commit при успехе, rollback при исключении."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


# scope="function": commit до отправки ответа, иначе ошибка commit не дойдёт до клиента.
DbSession = Annotated[AsyncSession, Depends(get_session, scope="function")]

# scope="request": сессия живёт до конца отправки ответа — для SSE-стрима, который пишет в БД
# после возврата из эндпоинта. Фиксировать изменения стрим должен сам (commit), до `done`.
# Между транзакциями сессия соединения не держит: стрим закрывает транзакцию перед долгим
# ожиданием (ответ модели), иначе одновременных стримов не больше размера пула (ADR-0034).
StreamDbSession = Annotated[AsyncSession, Depends(get_session, scope="request")]
