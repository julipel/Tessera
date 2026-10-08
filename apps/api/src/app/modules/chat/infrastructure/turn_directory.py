"""Ходы всех процессов API: Redis (общий для процессов) и в памяти (тесты), ADR-0035."""

import asyncio
from collections.abc import Callable
from uuid import UUID

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.modules.shared.kernel import TenantId

logger = structlog.get_logger(__name__)

CANCEL_CHANNEL = "turns:cancel"
# Ключ хода живёт, пока ход идёт; TTL — на случай падения процесса до unregister.
TURN_TTL_S = 15 * 60

type OnCancel = Callable[[UUID], None]


def _owner(tenant_id: TenantId, conversation_id: UUID) -> str:
    return f"{tenant_id}:{conversation_id}"


class RedisTurnDirectory:
    """`turn:<turn_id>` = `<tenant_id>:<conversation_id>` на время хода; отмена — сообщение
    с turn_id в канал `turns:cancel`, его слушают все процессы (`listen`), отменяет процесс
    хода. Redis недоступен — warning в лог: отмена только в процессе со стримом (ADR-0035).

    `subscriber` — отдельный клиент для подписки: у основного короткий таймаут чтения
    (лимиты, ADR-0030), а подписка ждёт сообщений бесконечно."""

    def __init__(self, redis: Redis, subscriber: Redis, retry_s: float = 1.0) -> None:
        self._redis = redis
        self._subscriber = subscriber
        self._retry_s = retry_s

    async def register(self, turn_id: UUID, tenant_id: TenantId, conversation_id: UUID) -> None:
        try:
            await self._redis.set(_key(turn_id), _owner(tenant_id, conversation_id), ex=TURN_TTL_S)
        except (RedisError, OSError) as e:
            logger.warning("turn_directory_unavailable", op="register", error=str(e))

    async def unregister(self, turn_id: UUID) -> None:
        try:
            await self._redis.delete(_key(turn_id))
        except (RedisError, OSError) as e:
            logger.warning("turn_directory_unavailable", op="unregister", error=str(e))

    async def request_cancel(
        self, turn_id: UUID, tenant_id: TenantId, conversation_id: UUID
    ) -> bool:
        try:
            owner = await self._redis.get(_key(turn_id))
            if owner is None or owner.decode() != _owner(tenant_id, conversation_id):
                return False
            await self._redis.publish(CANCEL_CHANNEL, str(turn_id))
        except (RedisError, OSError) as e:
            logger.warning("turn_directory_unavailable", op="cancel", error=str(e))
            return False
        return True

    async def listen(self, on_cancel: OnCancel) -> None:
        """Слушать отмены, пока задачу не отменят; Redis недоступен — переподключаться."""
        while True:
            try:
                async with self._subscriber.pubsub(ignore_subscribe_messages=True) as pubsub:
                    await pubsub.subscribe(CANCEL_CHANNEL)
                    while True:
                        # С таймаутом, а не блокирующее чтение: так проходят health check.
                        message = await pubsub.get_message(timeout=1.0)
                        if message is not None:
                            _deliver(message["data"], on_cancel)
            except (RedisError, OSError) as e:
                logger.warning("turn_cancel_listener_failed", error=str(e))
                await asyncio.sleep(self._retry_s)


def _key(turn_id: UUID) -> str:
    return f"turn:{turn_id}"


def _deliver(data: bytes, on_cancel: OnCancel) -> None:
    try:
        turn_id = UUID(data.decode())
    except ValueError:
        logger.warning("turn_cancel_malformed", data=data[:64].decode(errors="replace"))
        return
    on_cancel(turn_id)


class InMemoryTurnDirectory:
    """Тот же каталог в памяти для тестов: несколько реестров на одном экземпляре — как
    несколько процессов API на одном Redis. Подписчики — `subscribe` вместо `listen`."""

    def __init__(self) -> None:
        self._owners: dict[UUID, str] = {}
        self._listeners: list[OnCancel] = []

    def subscribe(self, on_cancel: OnCancel) -> None:
        self._listeners.append(on_cancel)

    async def register(self, turn_id: UUID, tenant_id: TenantId, conversation_id: UUID) -> None:
        self._owners[turn_id] = _owner(tenant_id, conversation_id)

    async def unregister(self, turn_id: UUID) -> None:
        self._owners.pop(turn_id, None)

    async def request_cancel(
        self, turn_id: UUID, tenant_id: TenantId, conversation_id: UUID
    ) -> bool:
        if self._owners.get(turn_id) != _owner(tenant_id, conversation_id):
            return False
        for on_cancel in self._listeners:
            on_cancel(turn_id)
        return True
