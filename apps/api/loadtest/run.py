"""`make loadtest` → `python -m loadtest.run --base-url ... --widget-key ...`.

N посетителей (не больше C одновременно) создают диалог и делают M ходов подряд. Сводка —
в консоль, сырые замеры и сводка — в `loadtest/reports/<время>.json` от корня репозитория.
Код выхода: 0 — все ходы завершены, 1 — есть ошибки.
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from loadtest.client import Scenario, run_load
from loadtest.stats import Summary, render, summarize

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_DIR = REPO_ROOT / "loadtest" / "reports"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="loadtest", description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--widget-key", required=True)
    parser.add_argument("--dialogs", type=int, default=50, help="посетителей всего")
    parser.add_argument("--concurrency", type=int, help="одновременно; по умолчанию = --dialogs")
    parser.add_argument("--turns", type=int, default=3, help="ходов на диалог")
    parser.add_argument("--text", default="Подскажите уход для сухой кожи")
    parser.add_argument("--ramp-s", type=float, default=0.0, help="растянуть старты на N секунд")
    parser.add_argument("--timeout-s", type=float, default=120.0, help="таймаут запроса")
    parser.add_argument("--label", default="", help="метка прогона в отчёте")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> tuple[Summary, dict[str, object]]:
    scenario = Scenario(widget_key=args.widget_key, turns=args.turns, text=args.text)
    concurrency = args.concurrency or args.dialogs
    # Пул соединений клиента не должен быть узким местом: по соединению на посетителя.
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=args.timeout_s, limits=limits, trust_env=False
    ) as client:
        start = time.perf_counter()
        visitors = await run_load(
            client, scenario, dialogs=args.dialogs, concurrency=concurrency, ramp_s=args.ramp_s
        )
        wall_s = time.perf_counter() - start
    summary = summarize(
        [turn for v in visitors for turn in v.turns],
        [v.create_s for v in visitors if v.create_s is not None],
        [v.create_error for v in visitors if v.create_error is not None],
        wall_s,
    )
    report: dict[str, object] = {
        "label": args.label,
        "params": {
            "base_url": args.base_url,
            "dialogs": args.dialogs,
            "concurrency": concurrency,
            "turns": args.turns,
            "ramp_s": args.ramp_s,
        },
        "wall_s": wall_s,
        "summary": asdict(summary),
        "visitors": [asdict(v) for v in visitors],
    }
    return summary, report


def write_report(report: dict[str, object], reports_dir: Path = REPORTS_DIR) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary, report = asyncio.run(run(args))
    print(render(summary))
    print(f"отчёт: {write_report(report).relative_to(REPO_ROOT)}")
    return 0 if summary.error_rate == 0 and not summary.create_errors else 1


if __name__ == "__main__":
    sys.exit(main())
