"""`make eval [f=...]` → `python -m evals.run [--filter ...]`.

Код выхода: 0 — все диалоги прошли, 1 — есть проваленные или упавшие, 2 — не загрузились
диалоги или нечего запускать.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from app.knowledge_wiring import build_knowledge_services
from app.logs import configure_logging
from app.modules.agent.public import PLATFORM_PROMPT_VERSION, LLMClients
from app.modules.chat.public import builtin_turn_agent
from app.settings import Settings
from evals.dialogs import DIALOGS_DIR, InvalidDialogError, load_dialogs, select_dialogs
from evals.judge import JUDGE_PROMPT_VERSION, Judge
from evals.knowledge_inject import InjectingKnowledge
from evals.report import REPORTS_DIR, RunInfo, render_summary, totals, write_report
from evals.runner import TENANTS_DIR, DialogRunner, MemoryLeads, TenantConfigs, run_dialogs
from evals.tenant_data import connect_tenant_data


def llm_clients(settings: Settings) -> LLMClients:
    """Ключи провайдеров из env — как в `create_app`."""
    return LLMClients(
        openai_api_key=_secret(settings.openai_api_key),
        openai_base_url=settings.openai_base_url,
        openai_compatible_api_key=_secret(settings.openai_compatible_api_key),
        openai_compatible_base_url=settings.openai_compatible_base_url,
        anthropic_api_key=_secret(settings.anthropic_api_key),
        anthropic_base_url=settings.anthropic_base_url,
    )


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value else None


async def main_async(args: argparse.Namespace) -> int:
    try:
        dialogs = select_dialogs(load_dialogs(args.dialogs), args.filter)
    except InvalidDialogError as e:
        print(f"невалидный диалог: {e}", file=sys.stderr)
        return 2
    if not dialogs:
        print(f"нет диалогов в {args.dialogs} по фильтру {args.filter!r}", file=sys.stderr)
        return 2

    settings = Settings()
    if not args.verbose:
        # Логи агента перемешались бы со сводкой; предупреждения и ошибки оставляем.
        settings = settings.model_copy(update={"log_level": "WARNING"})
    configure_logging(settings)

    configs = TenantConfigs(args.tenants)
    llms = llm_clients(settings)
    judge = (
        None if args.no_judge else Judge(llms.for_provider, args.judge_provider, args.judge_model)
    )
    knowledge = build_knowledge_services(settings)
    tenants = await connect_tenant_data(settings, sorted({d.tenant for d in dialogs}))
    for tenant in sorted({d.tenant for d in dialogs}):
        source = "БД" if tenant in tenants.ids else f"нет данных: {tenants.error or 'нет в БД'}"
        print(f"Тенант {tenant}: знания и каталог — {source}", flush=True)
    # Подмешанные фрагменты диалогов (`knowledge_inject`) — поверх поиска по знаниям.
    search = InjectingKnowledge(knowledge.search) if knowledge else None
    agent = builtin_turn_agent(llms.for_provider, search, tenants.catalog, MemoryLeads())
    runner = DialogRunner(agent, configs, judge, tenant_ids=tenants.ids)
    started_at = datetime.now(UTC)
    print(f"Диалогов: {len(dialogs)}, параллельно: {args.concurrency}…", flush=True)
    try:
        results = await run_dialogs(runner, dialogs, args.concurrency)
    finally:
        if knowledge is not None:
            await knowledge.aclose()
        await tenants.aclose()

    models: dict[str, str] = {}
    judges: dict[str, str] = {}
    for tenant in sorted({d.tenant for d in dialogs}):
        try:
            config = configs.get(tenant)
        except Exception:
            models[tenant] = "конфиг не загружен"
            continue
        primary = config["model"]["primary"]
        models[tenant] = f"{primary['provider']}/{primary['name']}"
        if judge is not None:
            judges[tenant] = "/".join(judge.model_for(config))
    info = RunInfo(
        started_at=started_at,
        finished_at=datetime.now(UTC),
        platform_prompt_version=PLATFORM_PROMPT_VERSION,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        models=models,
        judges=judges,
        filter=args.filter,
    )
    report = write_report(results, info, args.reports)
    print(render_summary(results, report))
    return 0 if totals(results).ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run", description="Прогон эталонных диалогов")
    parser.add_argument("--filter", help="подстрока id диалога или тег")
    parser.add_argument(
        # 4 параллельных диалога упираются в лимит OpenAI по токенам в минуту (TPM).
        "--concurrency",
        type=int,
        default=2,
        help="диалогов параллельно",
    )
    parser.add_argument("--dialogs", type=Path, default=DIALOGS_DIR)
    parser.add_argument("--tenants", type=Path, default=TENANTS_DIR)
    parser.add_argument("--reports", type=Path, default=REPORTS_DIR)
    parser.add_argument(
        "--judge-provider",
        choices=["openai", "openai_compatible", "anthropic"],
        help="провайдер судьи (по умолчанию — model.primary тенанта)",
    )
    parser.add_argument("--judge-model", help="модель судьи (по умолчанию — model.primary тенанта)")
    parser.add_argument("--no-judge", action="store_true", help="без судьи: его проверки skipped")
    parser.add_argument("-v", "--verbose", action="store_true", help="логи агента (LOG_LEVEL)")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
