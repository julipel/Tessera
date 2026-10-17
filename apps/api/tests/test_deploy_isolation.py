"""Изоляция хранилищ в проде (REVIEW.md: D-1…D-3, дополнение ADR-0040): web и Caddy не видят
Postgres, Redis и Qdrant, хранилища — с паролями, api и worker — ролью приложения без
пароля владельца в окружении."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
STORES = ("postgres", "redis", "qdrant")


def _services() -> dict[str, Any]:
    compose = yaml.safe_load((ROOT / "deploy/compose.prod.yml").read_text())
    services: dict[str, Any] = compose["services"]
    return services


def test_internet_facing_services_do_not_reach_stores() -> None:
    services = _services()
    for name in ("web", "caddy"):
        assert services[name]["networks"] == ["edge"], name
    for name in STORES:
        assert services[name]["networks"] == ["backend"], name
    for name in ("api", "worker", "migrate"):
        assert "backend" in services[name]["networks"], name


def test_forwarded_ips_trusted_only_from_edge_subnet() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.prod.yml").read_text())
    subnet = compose["networks"]["edge"]["ipam"]["config"][0]["subnet"]
    assert compose["services"]["api"]["environment"]["FORWARDED_ALLOW_IPS"] == subnet


def test_app_uses_app_role_and_migrate_uses_owner() -> None:
    services = _services()
    for name in ("api", "worker"):
        env = services[name]["environment"]
        assert "//${APP_DB_USER:-tessera_app}:${APP_DB_PASSWORD:?}@" in env["DATABASE_URL"]
        # env_file несёт весь deploy/.env — пароль владельца перекрыт пустым.
        assert env["POSTGRES_PASSWORD"] == "", name
    migrate = services["migrate"]["environment"]
    assert "//${POSTGRES_USER:-app}:${POSTGRES_PASSWORD:?}@" in migrate["DATABASE_URL"]
    assert services["postgres"]["environment"]["APP_DB_PASSWORD"] == "${APP_DB_PASSWORD:?}"


def test_redis_and_qdrant_require_credentials() -> None:
    services = _services()
    redis_command = " ".join(services["redis"]["command"])
    assert "requirepass $$REDIS_PASSWORD" in redis_command
    assert "--requirepass" not in redis_command  # пароль не в argv
    assert services["redis"]["environment"]["REDIS_PASSWORD"] == "${REDIS_PASSWORD:?}"
    assert services["qdrant"]["environment"]["QDRANT__SERVICE__API_KEY"] == "${QDRANT_API_KEY:?}"
    for name in ("api", "worker"):
        env = services[name]["environment"]
        assert env["REDIS_URL"] == "redis://:${REDIS_PASSWORD:?}@redis:6379/0"
        assert env["QDRANT_API_KEY"] == "${QDRANT_API_KEY:?}"


def test_every_service_rotates_logs_and_has_memory_limit() -> None:
    """Без ротации json-file заполняет диск, без потолка памяти OOM-killer хоста бьёт
    случайный процесс (REVIEW.md: D-4)."""
    for name, service in _services().items():
        assert service["logging"]["options"]["max-size"], name
        if name != "migrate":  # одноразовый, тот же образ что api
            assert service["mem_limit"], name


def test_app_containers_are_hardened() -> None:
    """Корень только для чтения, без capabilities и повышения привилегий (REVIEW.md: D-9)."""
    services = _services()
    for name in ("api", "worker", "migrate", "web", "caddy"):
        service = services[name]
        assert service["read_only"] is True, name
        assert service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name
    assert services["caddy"]["cap_add"] == ["NET_BIND_SERVICE"]
    for name in STORES:
        assert "no-new-privileges:true" in services[name]["security_opt"], name


def test_dev_compose_ports_bound_to_localhost() -> None:
    """Docker публикует порты в обход ufw: Postgres с паролем app и Redis/Qdrant без
    авторизации на всех интерфейсах открыли бы данные сети (REVIEW.md: D-7)."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    ports = [port for service in compose["services"].values() for port in service.get("ports", [])]
    assert ports
    assert all(port.startswith("127.0.0.1:") for port in ports), ports


def test_caddy_requires_acme_email() -> None:
    """Адрес для писем Let's Encrypt про истекающий сертификат обязателен (REVIEW.md: D-13)."""
    caddy = _services()["caddy"]
    assert caddy["environment"]["ACME_EMAIL"] == "${ACME_EMAIL:?}"
    assert "email {$ACME_EMAIL}" in (ROOT / "deploy/Caddyfile").read_text()
