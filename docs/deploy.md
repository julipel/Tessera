# Деплой

Боевой стек на одном сервере: docker compose, Caddy с автоматическим TLS и один домен
(ADR-0040). Файлы — `deploy/`, образы — `apps/api/Dockerfile` и `apps/web/Dockerfile`.

```
             :443 ┌───────┐  /v1/*, /health  ┌─────┐   ┌──────────┐
посетитель ──────▶│ caddy │─────────────────▶│ api │──▶│ postgres │
                  │       │  остальное       ├─────┤   │ redis    │
                  │       │─────────────────▶│ web │   │ qdrant   │
                  └───────┘                  └─────┘   └──────────┘
                     migrate (alembic) — до api и worker; worker — синхронизация источников
```

## Требования

- VPS с Linux: 2 vCPU, 4 ГБ RAM, 20 ГБ диска. Docker Engine с плагином compose v2, git, make.
- Домен: A-запись (и AAAA, если есть IPv6) указывает на IP сервера. Входящие 80 и 443 (TCP
  и UDP) открыты — Let's Encrypt проверяет домен по 80.
- С сервера доступны API провайдеров LLM и эмбеддингов (`OPENAI_*` и т. д.).

## Первый запуск

```bash
git clone <репозиторий> tessera && cd tessera
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env    # DOMAIN, POSTGRES_PASSWORD (openssl rand -hex 24), ключи LLM
make deploy-up         # сборка образов, миграции, запуск; ждёт healthcheck
make deploy-seed       # тенанты из config/tenants; новые ключи виджета печатаются
make deploy-sync       # индексация источников (каталог, база знаний)
make deploy-admin-user email=you@example.com args="--superadmin"
```

При `APP_ENV=production` API и воркер не стартуют с паролем БД `app` (или пустым) и
с не-https адресом в `CORS_ORIGINS` — это значения разработки, забытые в env.

Ключи виджета: `make deploy-seed` печатает ключ тенанта при первом создании. Чтобы ключи
были заданы заранее, укажите `SEED_WIDGET_KEY=demo-beauty=wk_…,demo-garden=wk_…` в
`deploy/.env` до seed. Dev-ключи `wk_dev_*` в бою не используйте.

Проверка:
- `https://<домен>/health` → `{"status":"ok"}`;
- `https://<домен>/demo/shop/lumiere.html?key=<ключ demo-beauty>` — виджет открывается,
  ответ стримится, карточки показываются;
- `https://<домен>/admin/login` — диалог виден в «Диалогах» тенанта.

## Обновление

```bash
git pull
make deploy-up         # пересборка; migrate применяет новые миграции до старта api/worker
make deploy-seed       # если менялись config/tenants или data/pilot
```

Перед обновлением с миграциями сделайте бэкап (`make deploy-backup`). Простой при
обновлении — несколько секунд, пока перезапускаются api и web. При остановке api ждёт
идущие ответы до 15 с (`--timeout-graceful-shutdown` в `apps/api/Dockerfile`), более
долгие обрываются; затем отправляет накопленные трейсы Langfuse. Обычно это секунды, но
при недоступном Langfuse отправка ждёт `LANGFUSE_TIMEOUT_S` на каждую пачку, поэтому
остановка api ограничена `stop_grace_period: 60s` в `compose.prod.yml` — остаток трейсов
после этого теряется. `LANGFUSE_TIMEOUT_S` держите меньше 40 с, чтобы хотя бы одна пачка
успевала уйти.

## Эксплуатация

- `make deploy-ps` — состояние сервисов; `make deploy-logs svc=api` — логи (JSON, structlog).
- `make deploy-backup` — дамп Postgres в `backups/<время>.sql.gz` (каталог вне git).
  Восстановление:
  `gunzip -c backups/<файл>.sql.gz | docker compose -f deploy/compose.prod.yml --env-file deploy/.env exec -T postgres psql -U app app`.
- Сроки хранения данных посетителей (ADR-0043): воркер раз в сутки (03:17 UTC) удаляет
  диалоги, журнал хода и заявки старше `retention` из конфига тенанта. Перед первым запуском
  на накопленной базе — `make deploy-retention dry=1` (только счётчики); вручную —
  `make deploy-retention [t=<slug>]`. Итог — в логах воркера, событие `retention.purged`.
  Удалённое остаётся в бэкапах до их удаления: храните `backups/` не дольше нужного
  (срок ротации прибавляется к сроку хранения). В Langfuse задайте срок хранения трейсов
  в настройках проекта — не дольше срока диалогов.
- Qdrant отдельно не бэкапится: индекс восстанавливается `make deploy-sync` из источников.
- Файлы источников из админки лежат в томе `tessera-prod_knowledge-files`.
- Откат кода: `git checkout <коммит> && make deploy-up`. Миграции назад не откатываются
  сами; если новая миграция несовместима со старым кодом —
  `docker compose -f deploy/compose.prod.yml --env-file deploy/.env run --rm api alembic downgrade <ревизия>`
  до checkout.
- Смена домена: поменять `DOMAIN` и выполнить `make deploy-up` — web пересоберётся с новым
  `NEXT_PUBLIC_API_URL`. Ключи виджета перевыпускать не нужно: origin веб-чата разрешён
  всегда (ADR-0022), а сайтам тенантов — `allowed_origins` ключа.

## Безопасность

- `deploy/.env` — только на сервере, права `600`. В git — только `deploy/.env.example`.
- Наружу открыты только 80 и 443. Postgres, Redis, Qdrant и порт API не опубликованы.
- Лимиты частоты (ADR-0030) считаются по IP посетителя из `X-Forwarded-For` Caddy. API доверяет
  этому заголовку только из своей подсети compose (`COMPOSE_SUBNET` в `deploy/.env`, по
  умолчанию 172.30.240.0/24 — из стандартных пулов Docker; менять, если пересекается с
  сетями сервера или других проектов). Смена подсети у уже созданного стека — через
  `docker compose … down` и `make deploy-up` (сеть пересоздаётся). CDN перед Caddy — только с
  `trusted_proxies` на его подсети.
- Запрос к БД ограничен `DB_STATEMENT_TIMEOUT_S` (30 с), ожидание соединения пула —
  `DB_POOL_TIMEOUT_S`; миграции этого потолка не имеют.
- Caddy ставит на весь домен HSTS (без поддоменов), `nosniff` и `Referrer-Policy`; админка —
  со своей CSP с nonce и `frame-ancestors 'none'` (ADR-0044). Срок HSTS — `HSTS_MAX_AGE`:
  по умолчанию сутки; когда домен и HTTPS проверены после выкладки — `31536000` (год) и
  `make deploy-up`. Браузер запоминает HSTS с первого ответа: домен, переведённый обратно
  на http, откроется только по истечении срока.
- Поставьте лимит расходов у провайдера LLM: виджет публичный, каждый ход — платный запрос.

## Проверка стека локально

Тот же compose с `DOMAIN=localhost`: Caddy выпускает внутренний сертификат (браузер
предупредит о нём). Настройки приложения можно взять из своего `.env`. Порты 80 и 443
должны быть свободны.

```bash
cd deploy
export DOMAIN=localhost HSTS_MAX_AGE=0 POSTGRES_PASSWORD=local DEPLOY_ENV_FILE=../.env
docker compose -f compose.prod.yml up -d --build --wait
docker compose -f compose.prod.yml run --rm api python -m app.cli seed
docker compose -f compose.prod.yml run --rm api python -m app.cli sync
docker compose -f compose.prod.yml down      # -v — удалить и данные
```

Сборке нужен доступ к Docker Hub, ghcr.io, PyPI и npm (см. «Если сборка не идёт»).

## Если сборка не идёт

- `lookup registry-1.docker.io: no such host` / таймауты к ghcr.io, PyPI или npm — нет
  доступа к реестрам. В Docker Desktop задайте прокси (Settings → Resources → Proxies) или
  зеркало реестра в daemon.json (`registry-mirrors`).
- `pnpm install` падает с `fetch failed` — разовый сбой сети при скачивании пакетов;
  повторите сборку (слои с уже скачанным берутся из кэша).
- `нужен --build-arg NEXT_PUBLIC_API_URL` — образ web собирают не через compose, или в
  `deploy/.env` не задан `DOMAIN`.
- Caddy не получает сертификат (в `make deploy-logs svc=caddy` — ошибки ACME): проверьте
  A-запись домена и доступность порта 80 снаружи.
