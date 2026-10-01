#!/usr/bin/env python3
"""PreToolUse: запрещает ручное редактирование сгенерированных и чувствительных файлов.
Exit 2 + stderr -> Claude Code блокирует действие и показывает причину модели."""
import json
import sys
from pathlib import PurePosixPath

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path", "")
p = PurePosixPath(path.replace("\\", "/"))
parts = set(p.parts)

if "generated" in parts:
    print(
        f"{path}: сгенерированный файл. Измени JSON Schema в packages/contracts/schemas/ "
        "и запусти `make contracts`.",
        file=sys.stderr,
    )
    sys.exit(2)

if p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example":
    print(f"{path}: секреты не редактируются агентом. Обнови .env.example.", file=sys.stderr)
    sys.exit(2)

if p.name in {"uv.lock", "pnpm-lock.yaml"}:
    print(f"{path}: lock-файлы меняются только командами uv/pnpm по явному запросу.", file=sys.stderr)
    sys.exit(2)

sys.exit(0)
