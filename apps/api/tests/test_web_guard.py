"""SSRF-защита краулера `website`: адреса, сетевой бэкенд, транспорт httpx (ADR-0013)."""

from collections.abc import Iterable

import httpcore
import httpx
import pytest

from app.modules.knowledge.infrastructure.web_guard import (
    BlockedAddressError,
    GuardedNetworkBackend,
    GuardedTransport,
    is_public_address,
)


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2a00:1450:4010::8a", True),
        ("127.0.0.1", False),
        ("10.0.0.5", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("100.64.0.1", False),  # CGNAT
        ("169.254.169.254", False),  # метаданные облака
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("::", False),
        ("fe80::1%eth0", False),
        ("fc00::1", False),
        ("ff02::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped
        ("::ffff:8.8.8.8", True),
        ("64:ff9b::a00:1", False),  # NAT64 → 10.0.0.1
        ("64:ff9b::808:808", True),
        ("not-an-ip", False),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert is_public_address(address) is public


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, failing: Iterable[str] = ()) -> None:
        self.connected: list[tuple[str, int]] = []
        self.failing = set(failing)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 — интерфейс httpcore
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append((host, port))
        if host in self.failing:
            raise httpcore.ConnectError(f"{host} недоступен")
        return httpcore.AsyncNetworkStream()


def _resolver(*addresses: str):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


async def test_connects_to_resolved_public_address() -> None:
    inner = _RecordingBackend()
    backend = GuardedNetworkBackend(_resolver("93.184.216.34"), inner)
    await backend.connect_tcp("example.ru", 443)
    assert inner.connected == [("93.184.216.34", 443)]


async def test_tries_next_address_when_connect_fails() -> None:
    inner = _RecordingBackend(failing={"93.184.216.34"})
    backend = GuardedNetworkBackend(_resolver("93.184.216.34", "2a00:1450::1"), inner)
    await backend.connect_tcp("example.ru", 443)
    assert [h for h, _ in inner.connected] == ["93.184.216.34", "2a00:1450::1"]


@pytest.mark.parametrize(
    "addresses",
    [("127.0.0.1",), ("93.184.216.34", "10.0.0.1"), ()],
    ids=["private", "mixed", "empty"],
)
async def test_rejects_names_with_private_addresses(addresses: tuple[str, ...]) -> None:
    inner = _RecordingBackend()
    backend = GuardedNetworkBackend(_resolver(*addresses), inner)
    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp("rebind.example.ru", 80)
    assert inner.connected == []


async def test_resolves_on_every_connect() -> None:
    """DNS rebinding: при втором подключении имя указывает уже на внутренний адрес."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    async def resolve(host: str, port: int) -> list[str]:
        return next(answers)

    inner = _RecordingBackend()
    backend = GuardedNetworkBackend(resolve, inner)
    await backend.connect_tcp("rebind.example.ru", 80)
    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp("rebind.example.ru", 80)
    assert inner.connected == [("93.184.216.34", 80)]


async def test_rejects_other_ports_and_unix_sockets() -> None:
    backend = GuardedNetworkBackend(_resolver("93.184.216.34"), _RecordingBackend())
    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp("example.ru", 6379)
    with pytest.raises(BlockedAddressError):
        await backend.connect_unix_socket("/var/run/docker.sock")


async def test_guarded_transport_blocks_private_address() -> None:
    """Транспорт httpx действительно ходит через защищённый бэкенд."""
    inner = _RecordingBackend()
    transport = GuardedTransport(GuardedNetworkBackend(_resolver("127.0.0.1"), inner))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BlockedAddressError):
            await client.get("http://localhost.example.ru/")
    assert inner.connected == []
