"""Служебные команды: `python -m app.cli seed [файлы...]`, `python -m app.cli sync [slug...]`,
`python -m app.cli admin-user EMAIL …`, `python -m app.cli ensure-db`."""

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import UUID

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.modules.access.public import (
    MIN_PASSWORD_LENGTH,
    AdminMembershipRepository,
    AdminRole,
    InvalidEmailError,
    PasswordRequiredError,
    SqlAdminUserDirectory,
    WeakPasswordError,
    normalize_email,
    upsert_admin_user,
)
from app.modules.knowledge.public import (
    InvalidSourceDeclarationError,
    LocalSourceFileStore,
    MarkdownChunker,
    SourceDeclaration,
    SourceRecord,
    SourceRepository,
    SourceSyncRepository,
    SqlSyncStore,
    SyncStatus,
    build_web_client,
    create_qdrant_index,
    parse_source_declarations,
    run_sync_now,
    seed_sources,
    validate_source_config,
)
from app.modules.shared.public import TenantId, create_engine, create_session_factory
from app.modules.tenants.public import (
    AgentConfigRepository,
    InvalidTenantSpecError,
    SqlTenantDirectory,
    TenantSpec,
    WidgetKeyRepository,
    load_tenant_spec,
    seed_tenant,
)
from app.settings import Settings
from app.worker import build_connectors, build_embedder

# src/app/cli.py → корень репозитория на 4 уровня выше пакета (как в settings.py).
DEFAULT_TENANTS_DIR = Path(__file__).resolve().parents[4] / "config" / "tenants"


async def ensure_database(url: str) -> bool:
    """Создать базу из `url`, если её нет (через служебную `postgres`). True — создана."""
    target = make_url(url)
    admin = create_async_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
            return not exists
    finally:
        await admin.dispose()


def widget_keys_by_slug(value: str | None, slugs: list[str]) -> dict[str, str]:
    """Ключи виджета для seed: `slug=key[,slug=key…]` или просто `key` при одном тенанте.

    `key_hash` уникален, поэтому один ключ на несколько тенантов не годится. Slug, которых
    нет среди загружаемых тенантов, пропускаются: SEED_WIDGET_KEY из .env может описывать всех.
    """
    if not value:
        return {}
    if "=" not in value:
        if len(slugs) != 1:
            raise ValueError(
                "один ключ виджета на несколько тенантов: задайте slug=key[,slug=key…]"
            )
        return {slugs[0]: value}
    keys: dict[str, str] = {}
    for pair in value.split(","):
        slug, sep, key = (part.strip() for part in pair.partition("="))
        if not sep or not slug or not key:
            raise ValueError(f"ключ виджета не в формате slug=key: {pair!r}")
        keys[slug] = key
    return {slug: key for slug, key in keys.items() if slug in slugs}


def parse_grants(values: list[str]) -> dict[str, AdminRole]:
    """Роли в тенантах: `slug=role` (viewer, editor); последняя для slug побеждает."""
    grants: dict[str, AdminRole] = {}
    for value in values:
        slug, sep, role = (part.strip() for part in value.partition("="))
        if not sep or not slug or role not in AdminRole:
            roles = ", ".join(AdminRole)
            raise ValueError(f"роль не в формате slug=role ({roles}): {value!r}")
        grants[slug] = AdminRole(role)
    return grants


def read_password(
    environ: Mapping[str, str] = os.environ,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """Пароль из ADMIN_PASSWORD (для скриптов) или ввод дважды без эха."""
    if password := environ.get("ADMIN_PASSWORD"):
        return password
    password = prompt(f"Пароль (не короче {MIN_PASSWORD_LENGTH} символов): ")
    if prompt("Повторите пароль: ") != password:
        raise ValueError("пароли не совпадают")
    return password


async def admin_user(
    email: str,
    *,
    password: str | None,
    is_superadmin: bool | None,
    is_active: bool | None,
    grants: dict[str, AdminRole],
    revokes: list[str],
    settings: Settings,
) -> bool:
    """Создать или изменить пользователя админки. False — неизвестный slug тенанта."""
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            directory = SqlTenantDirectory(session)
            tenant_ids: dict[str, TenantId] = {}
            for slug in [*grants, *revokes]:
                tenant = await directory.get_by_slug(slug)
                if tenant is None:
                    print(f"{slug}: тенант не найден — сначала make seed", file=sys.stderr)
                    return False
                tenant_ids[slug] = tenant.id
            result = await upsert_admin_user(
                email,
                users=SqlAdminUserDirectory(session),
                memberships=AdminMembershipRepository(session),
                password=password,
                is_superadmin=is_superadmin,
                is_active=is_active,
                grants={tenant_ids[slug]: role for slug, role in grants.items()},
                revokes=[tenant_ids[slug] for slug in revokes],
            )
            roles = []
            for membership in result.memberships:
                tenant = await directory.get(membership.tenant_id)
                roles.append(f"{tenant.slug if tenant else membership.tenant_id}={membership.role}")
        user = result.user
        flags = [
            "создан" if result.created else "обновлён",
            *(["суперадмин"] if user.is_superadmin else []),
            *([] if user.is_active else ["отключён"]),
        ]
        print(f"{user.email}: {', '.join(flags)}; роли: {', '.join(roles) or 'нет'}")
        return True
    finally:
        await engine.dispose()


def load_specs(paths: list[Path]) -> list[tuple[TenantSpec, list[SourceDeclaration]]]:
    """Все описания разбираются до записи в БД: ошибка в любом — ничего не записано."""
    specs = []
    for path in paths:
        spec = load_tenant_spec(path.read_text(encoding="utf-8"))
        specs.append((spec, parse_source_declarations(spec.sources, path.parent)))
    return specs


async def seed(
    specs: list[tuple[TenantSpec, list[SourceDeclaration]]],
    widget_keys: dict[str, str],
    reset_widget_key: bool,
    settings: Settings,
) -> None:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            for spec, declarations in specs:
                result = await seed_tenant(
                    spec,
                    tenants=SqlTenantDirectory(session),
                    configs=AgentConfigRepository(session),
                    widget_keys=WidgetKeyRepository(session),
                    widget_key=widget_keys.get(spec.tenant.slug),
                    reset_widget_key=reset_widget_key,
                )
                state = "новая версия" if result.config_changed else "без изменений"
                print(
                    f"{result.tenant.slug}: AgentConfig v{result.active_config.version} "
                    f"active ({state})"
                )
                if result.new_widget_key:
                    print(f"{result.tenant.slug}: ключ виджета {result.new_widget_key}")
                sources = await seed_sources(
                    result.tenant.id,
                    declarations,
                    registry=SourceRepository(session),
                    files=LocalSourceFileStore(settings.knowledge_files_dir),
                    validate=validate_source_config,
                )
                for seeded in sources.seeded:
                    line = f"{result.tenant.slug}: источник {seeded.name} — {seeded.action}"
                    if (files := seeded.files) is not None:
                        line += (
                            f", файлы: скопировано {files.copied}, удалено {files.removed}, "
                            f"без изменений {files.unchanged}"
                        )
                    print(line)
                for name in sources.undeclared:
                    print(f"{result.tenant.slug}: источник {name} не объявлен в YAML (не удалён)")
    finally:
        await engine.dispose()


async def sync(slugs: list[str], names: list[str], full: bool, settings: Settings) -> bool:
    """Синхронизировать именованные источники тенантов в этом процессе (без воркера).
    False — тенант или источник не найден либо синхронизация завершилась ошибкой."""
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    web_client = build_web_client(
        user_agent=settings.crawler_user_agent, timeout_s=settings.crawler_timeout_s
    )
    embedder = build_embedder(settings)
    qdrant_key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    index = create_qdrant_index(
        settings.qdrant_url,
        collection=settings.qdrant_collection,
        dimensions=settings.embedding_dimensions,
        api_key=qdrant_key or None,
    )
    connectors = build_connectors(settings, web_client)
    ok = True
    try:
        await index.ensure_collection()
        for slug in slugs:
            sources = await _named_sources(session_factory, slug)
            if sources is None:
                print(f"{slug}: тенант не найден — сначала make seed", file=sys.stderr)
                ok = False
                continue
            tenant_id, by_name = sources
            for name in names:
                if name not in by_name:
                    print(f"{slug}: источник {name} не найден", file=sys.stderr)
                    ok = False
            for name, source_id in sorted(by_name.items()):
                if names and name not in names:
                    continue
                async with session_factory() as session:
                    sync_id = await run_sync_now(
                        tenant_id,
                        source_id,
                        SqlSyncStore(session),
                        connectors,
                        MarkdownChunker(),
                        embedder=embedder,
                        index=index,
                        full=full,
                    )
                async with session_factory() as session:
                    record = await SourceSyncRepository(session).get_or_raise(tenant_id, sync_id)
                line = f"{slug}: {name} — {record.status} {record.stats}"
                if record.error:
                    line += f": {record.error}"
                print(line)
                ok = ok and record.status is SyncStatus.SUCCEEDED
    finally:
        await web_client.http.aclose()
        await embedder.aclose()
        await index.aclose()
        await engine.dispose()
    return ok


async def _named_sources(
    session_factory: async_sessionmaker[AsyncSession], slug: str
) -> tuple[TenantId, dict[str, UUID]] | None:
    async with session_factory() as session:
        tenant = await SqlTenantDirectory(session).get_by_slug(slug)
        if tenant is None:
            return None
        records = await SourceRepository(session).list(tenant.id, SourceRecord.name.is_not(None))
    return tenant.id, {r.name: r.id for r in records if r.name}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    seed_cmd = sub.add_parser("seed", help="загрузить тенантов из YAML")
    seed_cmd.add_argument("paths", nargs="*", type=Path, help="по умолчанию config/tenants/*.yaml")
    seed_cmd.add_argument(
        "--widget-key",
        help=(
            "ключи виджета новых тенантов: slug=key[,slug=key…] или key при одном тенанте "
            "(иначе SEED_WIDGET_KEY из env/.env; нет ключа — случайный)"
        ),
    )
    seed_cmd.add_argument(
        "--reset-widget-key",
        action="store_true",
        help="удалить ключи тенанта и выпустить новый (--widget-key / SEED_WIDGET_KEY / случайный)",
    )
    sync_cmd = sub.add_parser("sync", help="синхронизировать источники тенантов (без воркера)")
    sync_cmd.add_argument("slugs", nargs="*", help="по умолчанию — тенанты из config/tenants")
    sync_cmd.add_argument(
        "--source", action="append", default=[], help="имя источника (можно несколько)"
    )
    sync_cmd.add_argument(
        "--incremental",
        action="store_true",
        help="по курсору; по умолчанию полная — видит и смену конфига источника",
    )
    user_cmd = sub.add_parser(
        "admin-user",
        help="создать или изменить пользователя админки",
        description=(
            "Новому пользователю нужен пароль: ADMIN_PASSWORD из env или ввод с клавиатуры. "
            "Существующему пароль меняется только с --reset-password."
        ),
    )
    user_cmd.add_argument("email")
    user_cmd.add_argument(
        "--tenant",
        action="append",
        default=[],
        metavar="SLUG=ROLE",
        help="роль в тенанте: viewer или editor (можно несколько)",
    )
    user_cmd.add_argument(
        "--revoke", action="append", default=[], metavar="SLUG", help="снять роль в тенанте"
    )
    user_cmd.add_argument(
        "--superadmin",
        action=argparse.BooleanOptionalAction,
        help="все тенанты и пользователи (--no-superadmin — снять)",
    )
    user_cmd.add_argument(
        "--active",
        action=argparse.BooleanOptionalAction,
        help="--no-active — отключить вход, --active — включить",
    )
    user_cmd.add_argument("--reset-password", action="store_true", help="задать новый пароль")
    sub.add_parser("ensure-db", help="создать базу из DATABASE_URL, если её нет")
    args = parser.parse_args(argv)

    if args.command == "admin-user":
        return _admin_user_command(args)

    if args.command == "ensure-db":
        url = Settings().database_url
        created = asyncio.run(ensure_database(url))
        print(f"{make_url(url).database}: {'создана' if created else 'уже есть'}")
        return 0

    if args.command == "sync":
        try:
            slugs = args.slugs or [spec.tenant.slug for spec, _ in load_specs(_tenant_files())]
        except (InvalidTenantSpecError, InvalidSourceDeclarationError) as e:
            print(f"невалидное описание тенанта: {e}", file=sys.stderr)
            return 1
        ok = asyncio.run(sync(slugs, args.source, not args.incremental, Settings()))
        return 0 if ok else 1

    paths: list[Path] = args.paths or _tenant_files()
    if not paths:
        print(f"нет файлов тенантов в {DEFAULT_TENANTS_DIR}", file=sys.stderr)
        return 1
    try:
        settings = Settings()
        specs = load_specs(paths)
        # Settings читает и env, и .env — os.environ один .env не видит.
        widget_keys = widget_keys_by_slug(
            args.widget_key or settings.seed_widget_key,
            [spec.tenant.slug for spec, _ in specs],
        )
    except (InvalidTenantSpecError, InvalidSourceDeclarationError) as e:
        print(f"невалидное описание тенанта: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    asyncio.run(seed(specs, widget_keys, args.reset_widget_key, settings))
    return 0


def _admin_user_command(args: argparse.Namespace) -> int:
    settings = Settings()
    try:
        grants = parse_grants(args.tenant)
        password = None
        if args.reset_password or not asyncio.run(_admin_user_exists(args.email, settings)):
            password = read_password()
        ok = asyncio.run(
            admin_user(
                args.email,
                password=password,
                is_superadmin=args.superadmin,
                is_active=args.active,
                grants=grants,
                revokes=args.revoke,
                settings=settings,
            )
        )
    except (ValueError, InvalidEmailError, WeakPasswordError, PasswordRequiredError) as e:
        print(e, file=sys.stderr)
        return 1
    except EOFError:
        print("пароль не введён: без терминала задайте ADMIN_PASSWORD", file=sys.stderr)
        return 1
    return 0 if ok else 1


async def _admin_user_exists(email: str, settings: Settings) -> bool:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            users = SqlAdminUserDirectory(session)
            return await users.get_by_email(normalize_email(email)) is not None
    finally:
        await engine.dispose()


def _tenant_files() -> list[Path]:
    return sorted(DEFAULT_TENANTS_DIR.glob("*.yaml"))


if __name__ == "__main__":
    sys.exit(main())
