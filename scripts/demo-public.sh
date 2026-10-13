#!/usr/bin/env bash
# Публичное демо через туннель (P9-00b): `make demo-public`, остановка — Ctrl+C.
#
# Два quick tunnel cloudflared (web :3000, API :8000) со случайными адресами
# https://*.trycloudflare.com, затем API, worker и production-сборка web с этими адресами.
# Витрины (public/demo/shop) открываются на origin веб-чата — он в cors_origins API, и
# GET /v1/public/widget разрешает его в frame-ancestors (ADR-0022, дополнение P9-00b).
# Нужны `make up` и данные тенантов (`make pilot`); dev-серверы на 3000/8000 — остановить.
#
# Ключи виджета в ссылках — DEMO_BEAUTY_KEY / DEMO_GARDEN_KEY, иначе SEED_WIDGET_KEY
# (env или .env, как у `make seed`). Подробности и сценарии показа — docs/demo.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_DIR="$ROOT/apps/api"
WEB_DIR="$ROOT/apps/web"
LOG_DIR="$ROOT/.demo"
HOST=127.0.0.1
API_PORT=8000
WEB_PORT=3000
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$HOST" no_proxy="${no_proxy:+$no_proxy,}$HOST"

command -v cloudflared >/dev/null || {
  echo "нет cloudflared — установка в docs/demo.md" >&2
  exit 2
}
for port in $API_PORT $WEB_PORT; do
  if ss -ltn "sport = :$port" | grep -q LISTEN; then
    echo "порт $port занят — остановите dev-сервер (make dev-api / make dev-web)" >&2
    exit 2
  fi
done

mkdir -p "$LOG_DIR"
pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Адрес quick tunnel cloudflared печатает в лог при старте; результат — в tunnel_url
# (не через $(…): процесс туннеля должен остаться в pids этой оболочки).
# api.trycloudflare.com отвечает на запрос туннеля бывает дольше таймаута cloudflared —
# тогда процесс завершается с `failed to request quick Tunnel`, и пробуем снова.
tunnel() {
  local name=$1 port=$2 log="$LOG_DIR/tunnel-$1.log" url="" attempt pid
  for attempt in 1 2 3 4 5; do
    cloudflared tunnel --no-autoupdate --url "http://$HOST:$port" >"$log" 2>&1 &
    pid=$!
    for _ in $(seq 60); do
      # api.trycloudflare.com — адрес, у которого cloudflared запрашивает туннель (он же в ошибках).
      url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" | grep -v '//api\.' | head -1 || true)
      [[ -n "$url" ]] && break
      grep -q 'failed to request quick Tunnel' "$log" && break
      sleep 0.5
    done
    if [[ -n "$url" ]]; then
      pids+=("$pid")
      break
    fi
    # Неудачную попытку дождаться здесь: иначе финальный `wait -n` сработает на ней.
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    echo "туннель $name: попытка $attempt не удалась, повтор…" >&2
  done
  if [[ -z "$url" ]]; then
    echo "туннель $name не поднялся — см. $log (сеть: docs/demo.md)" >&2
    exit 2
  fi
  tunnel_url=$url
}

# Адрес выдаётся до подключения к edge Cloudflare (порт 7844): без соединения ссылка не откроется.
# До 90 с на туннель: при нестабильной сети cloudflared перебирает адреса edge с паузами.
wait_registered() {
  local log
  for log in "$LOG_DIR"/tunnel-*.log; do
    for _ in $(seq 180); do
      grep -q 'Registered tunnel connection' "$log" && continue 2
      sleep 0.5
    done
    echo "туннель не подключился к Cloudflare (порт 7844) — см. $log и docs/demo.md" >&2
    exit 2
  done
}

wait_up() {
  local url=$1 log=$2
  for _ in $(seq 120); do
    curl -sf --noproxy '*' --max-time 2 "$url" >/dev/null && return 0
    sleep 1
  done
  echo "не поднялся $url — см. $log" >&2
  exit 2
}

echo "туннели…"
tunnel api $API_PORT
api_url=$tunnel_url
tunnel web $WEB_PORT
web_url=$tunnel_url
wait_registered

cd "$API_DIR"
uv run alembic upgrade head >"$LOG_DIR/migrate.log" 2>&1 || {
  echo "миграции не прошли (make up?) — см. $LOG_DIR/migrate.log" >&2
  exit 2
}
# Свой веб-чат за туннелем и локально; IP посетителя для лимитов — из X-Forwarded-For
# cloudflared (ADR-0030).
export CORS_ORIGINS="[\"$web_url\",\"http://localhost:$WEB_PORT\"]"
uv run uvicorn app.main:app --host $HOST --port $API_PORT \
  --proxy-headers --forwarded-allow-ips $HOST >"$LOG_DIR/api.log" 2>&1 &
pids+=($!)
uv run arq app.worker.WorkerSettings >"$LOG_DIR/worker.log" 2>&1 &
pids+=($!)
keys=$(uv run python -c '
import os
from app.cli import widget_keys_by_slug
from app.settings import Settings
slugs = ["demo-beauty", "demo-garden"]
try:
    keys = widget_keys_by_slug(Settings().seed_widget_key, slugs)
except ValueError:
    keys = {}
env = {"demo-beauty": "DEMO_BEAUTY_KEY", "demo-garden": "DEMO_GARDEN_KEY"}
print(" ".join(os.environ.get(env[s]) or keys.get(s) or "wk_…" for s in slugs))
')
read -r beauty_key garden_key <<<"$keys"
wait_up "http://$HOST:$API_PORT/health" "$LOG_DIR/api.log"

echo "сборка web (NEXT_PUBLIC_API_URL=$api_url)…"
cd "$WEB_DIR"
export NEXT_PUBLIC_API_URL="$api_url"
pnpm build >"$LOG_DIR/web-build.log" 2>&1 || {
  echo "сборка web не прошла — см. $LOG_DIR/web-build.log" >&2
  exit 2
}
pnpm start -H $HOST -p $WEB_PORT >"$LOG_DIR/web.log" 2>&1 &
pids+=($!)
wait_up "http://$HOST:$WEB_PORT/demo/shop/index.html" "$LOG_DIR/web.log"
# DNS нового адреса trycloudflare появляется не сразу.
wait_up "$web_url/demo/shop/index.html" "$LOG_DIR/tunnel-web.log"

cat <<EOF

Демо доступно (ссылки живут, пока работает скрипт):
  Lumière:       $web_url/demo/shop/lumiere.html?key=$beauty_key
  Зелёный угол:  $web_url/demo/shop/garden.html?key=$garden_key
  Все витрины:   $web_url/demo/shop/index.html
  Админка:       $web_url/admin/login
  API:           $api_url
Логи — $LOG_DIR. Остановить — Ctrl+C.
EOF
# Упал любой процесс — гасим остальные: полуживое демо хуже остановленного.
wait -n
echo "один из процессов демо завершился — см. $LOG_DIR/*.log" >&2
exit 1
