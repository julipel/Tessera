"""Ходы всех процессов API: Redis (общий для процессов) и в памяти (тесты), ADR-0035."""

import asyncio
from collections.abc import Callable
from uuid import UUID

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.modules.chat.domain.ports import TURN_TTL_S
from app.modules.shared.kernel import TenantId

logger = structlog.get_logger(__name__)

CANCEL_CHANNEL = "turns:cancel"

type OnCancel = Callable[[UUID], None]


def _owner(tenant_id: TenantId, conversation_id: UUID) -> str:
    return f"{tenant_id}:{conversation_id}"


# Снять ключ диалога, только если его держит этот ход: ход с истёкшим ключом не освободит
# диалог следующему ходу.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


class RedisTurnDirectory:
    """`turn:<turn_id>` = `<tenant_id>:<conversation_id>` на время хода; отмена — сообщение
    с turn_id в канал `turns:cancel`, его слушают все процессы (`listen`), отменяет процесс
    хода. Redis недоступен — warning в лог: отмена только в процессе со стримом (ADR-0035).

    `conversation_turn:<tenant_id>:<conversation_id>` = turn_id — диалог занят ходом
    (`SET NX`, ADR-0041). Redis недоступен — диалог занимается без проверки других процессов.

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

    async def acquire_conversation(
        self, tenant_id: TenantId, conversation_id: UUID, turn_id: UUID
    ) -> bool:
        try:
            acquired = await self._redis.set(
                _conversation_key(tenant_id, conversation_id), str(turn_id), nx=True, ex=TURN_TTL_S
            )
        except (RedisError, OSError) as e:
            logger.warning("turn_directory_unavailable", op="acquire", error=str(e))
            return True
        return bool(acquired)

    async def release_conversation(
        self, tenant_id: TenantId, conversation_id: UUID, turn_id: UUID
    ) -> None:
        try:
            await self._redis.eval(  # type: ignore[misc]
                _RELEASE_SCRIPT, 1, _conversation_key(tenant_id, conversation_id), str(turn_id)
            )
        except (RedisError, OSError) as e:
            logger.warning("turn_directory_unavailable", op="release", error=str(e))

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


def _conversation_key(tenant_id: TenantId, conversation_id: UUID) -> str:
    return f"conversation_turn:{_owner(tenant_id, conversation_id)}"


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
        self._conversations: dict[str, UUID] = {}
        self._listeners: list[OnCancel] = []

    def subscribe(self, on_cancel: OnCancel) -> None:
        self._listeners.append(on_cancel)

    async def register(self, turn_id: UUID, tenant_id: TenantId, conversation_id: UUID) -> None:
        self._owners[turn_id] = _owner(tenant_id, conversation_id)

    async def unregister(self, turn_id: UUID) -> None:
        self._owners.pop(turn_id, None)

    async def acquire_conversation(
        self, tenant_id: TenantId, conversation_id: UUID, turn_id: UUID
    ) -> bool:
        owner = _owner(tenant_id, conversation_id)
        return self._conversations.setdefault(owner, turn_id) == turn_id

    async def release_conversation(
        self, tenant_id: TenantId, conversation_id: UUID, turn_id: UUID
    ) -> None:
        owner = _owner(tenant_id, conversation_id)
        if self._conversations.get(owner) == turn_id:
            del self._conversations[owner]

    async def request_cancel(
        self, turn_id: UUID, tenant_id: TenantId, conversation_id: UUID
    ) -> bool:
        if self._owners.get(turn_id) != _owner(tenant_id, conversation_id):
            return False
        for on_cancel in self._listeners:
            on_cancel(turn_id)
        return True
