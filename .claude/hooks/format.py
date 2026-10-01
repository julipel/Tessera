#!/usr/bin/env python3
"""PostToolUse: автоформатирование изменённого файла. Никогда не блокирует —
если инструменты ещё не установлены (ранний этап), молча выходит."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path", "")
if not path or not Path(path).exists():
    sys.exit(0)

root = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
f = Path(path).resolve()


def run(cmd: list[str], cwd: Path) -> None:
    try:
        subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=50)
    except Exception:
        pass


if f.suffix == ".py" and shutil.which("uv"):
    api = root / "apps" / "api"
    cwd = api if (api / "pyproject.toml").exists() else root
    run(["uv", "run", "ruff", "format", str(f)], cwd)
    run(["uv", "run", "ruff", "check", "--fix", str(f)], cwd)
elif f.suffix in {".ts", ".tsx", ".js", ".jsx", ".json", ".css"} and shutil.which("pnpm"):
    web = root / "apps" / "web"
    if (web / "package.json").exists() and web in f.parents:
        run(["pnpm", "exec", "prettier", "--write", str(f)], web)

sys.exit(0)
