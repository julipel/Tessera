#!/usr/bin/env bash
# Генерация контрактов из JSON Schema (ADR-0005, docs/contracts.md).
#   scripts/contracts.sh generate — перегенерировать Pydantic и TS на месте
#   scripts/contracts.sh check    — упасть, если закоммиченный generated неактуален
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMAS="$ROOT/packages/contracts/schemas"
API="$ROOT/apps/api"
PY_OUT="$API/src/app/contracts/generated"
TS_OUT="$ROOT/apps/web/src/contracts/generated"

generate_into() {
  local py_out="$1" ts_out="$2" staging
  staging="$(mktemp -d)"
  # В папке схем лежит README.md — генератору отдаём только сами схемы. Имя подпапки
  # постоянное: оно попадает в шапку generated/__init__.py.
  mkdir "$staging/schemas"
  cp "$SCHEMAS"/*.schema.json "$staging/schemas/"

  (cd "$API" && uv run --locked datamodel-codegen \
    --input "$staging/schemas" \
    --input-file-type jsonschema \
    --output "$py_out" \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.12 \
    --disable-timestamp \
    --use-annotated \
    --use-union-operator \
    --use-standard-collections \
    --enum-field-as-literal all \
    --use-double-quotes \
    --use-schema-description \
    --use-field-description \
    --use-title-as-name \
    --formatters builtin)
  # --isolated: результат не зависит от того, где лежит папка (временная или в репо).
  (cd "$API" && uv run --locked ruff format --isolated --line-length 100 --quiet "$py_out")
  rm -rf "$staging"

  (cd "$ROOT/packages/contracts" && node scripts/generate-ts.mjs "$ts_out")
}

case "${1:-}" in
  generate)
    rm -rf "$PY_OUT" "$TS_OUT"
    generate_into "$PY_OUT" "$TS_OUT"
    echo "contracts: сгенерировано в $PY_OUT и $TS_OUT"
    ;;
  check)
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    generate_into "$tmp/py" "$tmp/ts"
    if ! diff -r -x __pycache__ "$tmp/py" "$PY_OUT" || ! diff -r "$tmp/ts" "$TS_OUT"; then
      echo "contracts: сгенерированный код неактуален — запустите \`make contracts\`" >&2
      exit 1
    fi
    echo "contracts: актуальны"
    ;;
  *)
    echo "usage: $0 generate|check" >&2
    exit 2
    ;;
esac
