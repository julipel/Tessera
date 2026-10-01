#!/usr/bin/env python3
"""Stop: перед завершением ответа прогоняет `make check-fast`, если есть изменения.
При ошибках — exit 2, и Claude Code продолжает работу, чтобы их исправить.
stop_hook_active защищает от бесконечного цикла."""
import json
import os
import subprocess
import sys
from pathlib import Path

data = json.load(sys.stdin)
if data.get("stop_hook_active"):
    sys.exit(0)

root = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
makefile = root / "Makefile"
if not makefile.exists() or "check-fast:" not in makefile.read_text(encoding="utf-8"):
    sys.exit(0)

try:
    diff = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=20
    ).stdout.strip()
except Exception:
    sys.exit(0)
if not diff:
    sys.exit(0)

res = subprocess.run(
    ["make", "check-fast"], cwd=root, capture_output=True, text=True, timeout=280
)
if res.returncode != 0:
    out = (res.stdout + "\n" + res.stderr)[-4000:]
    print("`make check-fast` не прошёл. Исправь ошибки перед завершением:\n" + out, file=sys.stderr)
    sys.exit(2)
sys.exit(0)
