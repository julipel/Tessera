"""Служебные команды: `python -m app.cli seed [файлы...]`."""

import argparse
import asyncio
import sys
from pathlib import Path

from app.modules.shared.public import create_engine, create_session_factory
from app.modules.tenants.public import (
    AgentConfigRepository,
    InvalidTenantSpecError,
    SqlTenantDirectory,
    WidgetKeyRepository,
    load_tenant_spec,
    seed_tenant,
)
from app.settings import Settings

# src/app/cli.py → корень репозитория на 4 уровня выше пакета (как в settings.py).
DEFAULT_TENANTS_DIR = Path(__file__).resolve().parents[4] / "config" / "tenants"


async def seed(
    paths: list[Path], widget_key: str | None, reset_widget_key: bool, settings: Settings
) -> None:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            for path in paths:
                spec = load_tenant_spec(path.read_text(encoding="utf-8"))
                result = await seed_tenant(
                    spec,
                    tenants=SqlTenantDirectory(session),
                    configs=AgentConfigRepository(session),
                    widget_keys=WidgetKeyRepository(session),
                    widget_key=widget_key,
                    reset_widget_key=reset_widget_key,
                )
                state = "новая версия" if result.config_changed else "без изменений"
                print(
                    f"{result.tenant.slug}: AgentConfig v{result.active_config.version} "
                    f"active ({state})"
                )
                if result.new_widget_key:
                    print(f"{result.tenant.slug}: ключ виджета {result.new_widget_key}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    seed_cmd = sub.add_parser("seed", help="загрузить тенантов из YAML")
    seed_cmd.add_argument("paths", nargs="*", type=Path, help="по умолчанию config/tenants/*.yaml")
    seed_cmd.add_argument(
        "--widget-key",
        help="ключ виджета для нового тенанта (иначе SEED_WIDGET_KEY из env/.env или случайный)",
    )
    seed_cmd.add_argument(
        "--reset-widget-key",
        action="store_true",
        help="удалить ключи тенанта и выпустить новый (--widget-key / SEED_WIDGET_KEY / случайный)",
    )
    args = parser.parse_args(argv)

    paths: list[Path] = args.paths or sorted(DEFAULT_TENANTS_DIR.glob("*.yaml"))
    if not paths:
        print(f"нет файлов тенантов в {DEFAULT_TENANTS_DIR}", file=sys.stderr)
        return 1
    try:
        settings = Settings()
        # Settings читает и env, и .env — os.environ один .env не видит.
        widget_key = args.widget_key or settings.seed_widget_key or None
        asyncio.run(seed(paths, widget_key, args.reset_widget_key, settings))
    except InvalidTenantSpecError as e:
        print(f"невалидное описание тенанта: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
