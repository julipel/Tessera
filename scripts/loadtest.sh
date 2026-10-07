#!/usr/bin/env bash
# Нагрузочный тест API чата (P7-05): `make loadtest [n=50] [c=...] [turns=3]`.
#
# По умолчанию поднимает свой стенд: база `app_load` (демо-тенант demo-beauty), мок OpenAI
# из e2e (apps/web/e2e/mock-llm.mjs) с задержкой первого токена MOCK_LLM_FIRST_TOKEN_MS
# и API на 127.0.0.2:8011 — меряется платформа (БД, агентный цикл, SSE), а не провайдер.
# Лимиты частоты и Langfuse выключены, как в e2e. Нужен `make up` (Postgres).
#
# LOADTEST_BASE_URL=http://... LOADTEST_WIDGET_KEY=wk_... — прогон против уже запущенного API
# (например, dev с настоящей моделью: это платные запросы — малые n).
# Остальные аргументы уходят в `python -m loadtest.run` (--ramp-s, --text, --label, ...).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_DIR="$ROOT/apps/api"
HOST=127.0.0.2
API_PORT=8011
MOCK_PORT=8012
LOG_DIR="$ROOT/loadtest/reports"

if [[ -n "${LOADTEST_BASE_URL:-}" ]]; then
  base_url="$LOADTEST_BASE_URL" key="${LOADTEST_WIDGET_KEY:?нужен LOADTEST_WIDGET_KEY}"
  cd "$API_DIR" && exec uv run python -m loadtest.run --base-url "$base_url" --widget-key "$key" "$@"
fi

KEY=wk_load_demo_beauty
export APP_ENV="${LOADTEST_APP_ENV:-staging}"
export DATABASE_URL="${LOADTEST_DATABASE_URL:-postgresql+asyncpg://app:app@localhost:5432/app_load}"
export OPENAI_API_KEY=sk-load OPENAI_BASE_URL="http://$HOST:$MOCK_PORT/v1"
export RATE_TURNS_PER_IP_PER_MIN=0 RATE_TURNS_PER_TENANT_PER_MIN=0 RATE_CONVERSATIONS_PER_IP_PER_HOUR=0
export LANGFUSE_PUBLIC_KEY="" LANGFUSE_SECRET_KEY=""
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$HOST" no_proxy="${no_proxy:+$no_proxy,}$HOST"

cd "$API_DIR"
uv run python -m app.cli ensure-db
uv run alembic upgrade head >/dev/null
uv run python -m app.cli seed "$ROOT/config/tenants/demo-beauty.yaml" \
  --widget-key "$KEY" --reset-widget-key >/dev/null

mkdir -p "$LOG_DIR"
pids=()
trap 'kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null' EXIT
MOCK_LLM_HOST=$HOST MOCK_LLM_PORT=$MOCK_PORT MOCK_LLM_FIRST_TOKEN_MS="${MOCK_LLM_FIRST_TOKEN_MS:-500}" \
  node "$ROOT/apps/web/e2e/mock-llm.mjs" >"$LOG_DIR/mock-llm.log" 2>&1 &
pids+=($!)
uv run uvicorn app.main:app --host $HOST --port $API_PORT --workers "${LOADTEST_WORKERS:-1}" \
  >"$LOG_DIR/api.log" 2>&1 &
pids+=($!)

for url in "http://$HOST:$MOCK_PORT/health" "http://$HOST:$API_PORT/health"; do
  for _ in $(seq 60); do
    curl -sf --noproxy '*' "$url" >/dev/null && continue 2
    sleep 0.5
  done
  echo "не поднялся $url — см. $LOG_DIR/*.log" >&2
  exit 2
done

uv run python -m loadtest.run --base-url "http://$HOST:$API_PORT" --widget-key "$KEY" "$@"
