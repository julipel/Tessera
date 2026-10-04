"""Защита краулера `website` от SSRF (ADR-0013): подключения только к публичным адресам.

Проверка стоит в сетевом бэкенде httpcore — там, где имя хоста превращается в TCP-соединение:
бэкенд сам резолвит имя, отклоняет непубличные адреса и подключается к проверенному IP.
Поэтому она действует на каждый запрос и каждый редирект, и её не обойти DNS rebinding
(имя, которое при проверке и при подключении резолвится по-разному). TLS проверяется по
исходному имени хоста (httpcore передаёт его в SNI отдельно).
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable

import httpcore
import httpx

type Resolver = Callable[[str, int], Awaitable[list[str]]]

ALLOWED_PORTS = frozenset({80, 443})
# NAT64 (RFC 6052): Python считает префикс глобальным, но внутри — IPv4-адрес назначения.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


class BlockedAddressError(Exception):
    """Хост резолвится в непубличный адрес или порт не разрешён."""


def is_public_address(address: str) -> bool:
    """Глобально маршрутизируемый unicast-адрес; IPv4 внутри IPv6 (mapped, NAT64)
    проверяется как IPv4."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    elif isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


async def resolve_host(host: str, port: int) -> list[str]:
    """Все адреса хоста (A и AAAA) через резолвер ОС."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Сетевой бэкенд httpcore, подключающийся только к публичным адресам.

    Если хоть один адрес имени непубличный, имя отклоняется целиком: смешанные записи —
    типичный приём, чтобы проверка увидела публичный адрес, а подключение ушло на другой.
    """

    def __init__(
        self,
        resolver: Resolver = resolve_host,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self.resolver = resolver
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 — интерфейс httpcore
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if port not in ALLOWED_PORTS:
            raise BlockedAddressError(f"порт {port} не разрешён")
        try:
            async with asyncio.timeout(timeout):
                addresses = await self.resolver(host.strip("[]"), port)
        except (OSError, TimeoutError) as e:
            raise httpcore.ConnectError(f"не удалось разрешить {host}: {e}") from e
        blocked = [a for a in addresses if not is_public_address(a)]
        if blocked or not addresses:
            raise BlockedAddressError(f"{host} резолвится в непубличный адрес {blocked}")
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as e:
                last_error = e
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 — интерфейс httpcore
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedAddressError("unix-сокеты запрещены")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """`httpx.AsyncHTTPTransport` поверх пула httpcore с `GuardedNetworkBackend`.

    httpx не принимает сетевой бэкенд, поэтому пул подменяется (`_pool` — внутренний
    атрибут httpx 0.28; тест `test_guarded_transport_blocks_private_address` сломается,
    если это перестанет работать). Прокси из окружения не используются: прокси резолвил бы
    имена сам, в обход проверки.
    """

    def __init__(self, network_backend: GuardedNetworkBackend | None = None) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            network_backend=network_backend or GuardedNetworkBackend(),
        )
