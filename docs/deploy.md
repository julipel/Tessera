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
  и UDP) открыты — Let's Encrypt проверяет домен по 80. Запись `www` — туда же: Caddy
  выпускает сертификат и на `www.<домен>` и редиректит с него на основной домен (без неё —
  ошибки ACME для www в логах Caddy, основной домен работает).
- С сервера доступны API провайдеров LLM и эмбеддингов (`OPENAI_*` и т. д.).

## Первый запуск

```bash
git clone <репозиторий> tessera && cd tessera
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env    # DOMAIN, ACME_EMAIL, четыре секрета хранилищ (openssl rand -hex 24), ключи LLM
make deploy-up         # сборка образов, миграции, запуск; ждёт healthcheck
make deploy-seed       # тенанты из config/tenants; новые ключи виджета печатаются
make deploy-sync       # индексация источников (каталог, база знаний)
make deploy-admin-user email=you@example.com args="--superadmin"
```

Секреты хранилищ: `POSTGRES_PASSWORD` — владелец схемы (только migrate), `APP_DB_PASSWORD` —
роль приложения `APP_DB_USER` (api и worker, без DDL), `REDIS_PASSWORD`, `QDRANT_API_KEY`.
Роль приложения создаётся при первом запуске тома Postgres (`deploy/postgres/app-role.sh`).
Том, созданный до неё, или смена `APP_DB_PASSWORD` — `make deploy-db-roles` (сама
пересоздаёт postgres по новому `deploy/.env`), затем `make deploy-up`. Пароль владельца
в окружение api и worker не попадает. `app.cli ensure-db` (создание БД) — только для
разработки: у роли приложения нет `CREATEDB`.

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
docker image prune -f && docker builder prune -f   # старые образы и кеш сборки: диск небольшой
```

Seed не затирает конфиг, изменённый или откаченный в админке. Если YAML изменился, он пишет
«активна версия vN, выбранная в админке, — изменённый YAML не применён». Вернуть
управление YAML можно активацией в админке последней версии из seed, а YAML поверх применяет
`make deploy-seed args=--force-config`. Тогда вернутся и сроки хранения из YAML (ADR-0043),
поэтому сначала сверьте их с админкой.

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
- `make deploy-backup` — дамп Postgres `backups/<время>.sql.gz` и файлы источников из админки
  `backups/knowledge-<время>.tar.gz` (их синхронизация не восстановит). Каталог вне git,
  права 700, файлы 600: внутри ПДн посетителей и хэши паролей. Сбой дампа — ошибка make,
  недописанный файл удаляется. Копируйте `backups/` с сервера: на нём же они пропадут вместе
  с диском. Ротация — например, ежедневно из cron:
  `find backups -name '*.gz' -mtime +14 -delete` (срок прибавляется к сроку хранения, см. ниже).
- Восстановление (`D` — `docker compose -f deploy/compose.prod.yml --env-file deploy/.env`):
  дамп с `--clean --if-exists` — в ту же или пустую БД, api и worker на это время остановлены:
  ```bash
  $D stop api worker
  gunzip -c backups/<время>.sql.gz | $D exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$POSTGRES_DB"'
  $D run --rm --no-deps -T api tar -xzf - -C /srv/data/knowledge < backups/knowledge-<время>.tar.gz
  make deploy-db-roles && make deploy-up && make deploy-sync
  ```
  `deploy-sync` перестраивает индекс Qdrant (он не бэкапится).
- Сроки хранения данных посетителей (ADR-0043): воркер раз в сутки (03:17 UTC) удаляет
  диалоги, журнал хода и заявки старше `retention` из конфига тенанта. Перед первым запуском
  на накопленной базе — `make deploy-retention dry=1` (только счётчики); вручную —
  `make deploy-retention [t=<slug>]`. Итог — в логах воркера, событие `retention.purged`.
  Удалённое остаётся в бэкапах до их удаления: храните `backups/` не дольше нужного
  (срок ротации прибавляется к сроку хранения). В Langfuse задайте срок хранения трейсов
  в настройках проекта — не дольше срока диалогов.
- Qdrant отдельно не бэкапится: индекс восстанавливается `make deploy-sync` из источников
  (файлы из админки — из `knowledge-*.tar.gz`, том `tessera-prod_knowledge-files`).
- Логи контейнеров — `json-file` с ротацией (5 файлов по 20 МБ на сервис). Память ограничена
  `mem_limit` в `compose.prod.yml` (потолки для 4 ГБ RAM): при упоре OOM-killer завершает
  процесс внутри этого контейнера, а не случайный на хосте. Если это PID 1 (api, web), контейнер
  перезапускается (`restart`); у Postgres обычно гибнет один backend и сервер восстанавливается
  сам. Признак — `docker inspect <контейнер> --format '{{.State.OOMKilled}}'` и ошибки в логах.
  Redis — `maxmemory 192mb` без вытеснения: при переполнении запись отклоняется
  (`OOM command not allowed`). Тогда лимиты частоты чата и блокировка диалога не действуют
  (fail-open, ADR-0030; события `rate_limit_unavailable`, `turn_directory_unavailable`), а новые
  синхронизации не ставятся в очередь. Все ключи с TTL — обычно это не случается.
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
- Две сети compose (дополнение ADR-0040): `edge` — caddy, web, api; `backend` — api, worker,
  migrate, Postgres, Redis, Qdrant. Web и Caddy, открытые в интернет, хранилищ не видят.
  Redis — с паролем (arq разбирает задачи через pickle: запись в очередь равна выполнению
  кода в воркере), Qdrant — с API-ключом, api и worker ходят в Postgres ролью без DDL
  и прав суперпользователя. Телеметрия Qdrant выключена.
- Лимиты частоты (ADR-0030) считаются по IP посетителя из `X-Forwarded-For` Caddy. API доверяет
  этому заголовку только из подсети сети `edge` (`COMPOSE_SUBNET` в `deploy/.env`, по
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
- Контейнеры api, worker, migrate, web и caddy — с корневой ФС только для чтения, без
  capabilities (caddy — только `NET_BIND_SERVICE`) и без повышения привилегий; запись — в тома
  и tmpfs (`/tmp`, кэш Next). Код web принадлежит root. Caddy и web убирают `Server`
  и `X-Powered-By`, ставят `Permissions-Policy`; встраивать во фрейм можно только `/widget`.
- `/docs` и `/openapi.json` API в production выключены.

## Проверка стека локально

Тот же compose с `DOMAIN=localhost`: Caddy выпускает внутренний сертификат (браузер
предупредит о нём). Настройки приложения можно взять из своего `.env`. Порты 80 и 443
должны быть свободны.

```bash
cd deploy
export DOMAIN=localhost ACME_EMAIL=dev@localhost HSTS_MAX_AGE=0 POSTGRES_PASSWORD=local APP_DB_PASSWORD=local-app \
  REDIS_PASSWORD=local QDRANT_API_KEY=local DEPLOY_ENV_FILE=../.env
docker compose -f compose.prod.yml up -d --build --wait
docker compose -f compose.prod.yml run --rm api python -m app.cli seed
docker compose -f compose.prod.yml run --rm api python -m app.cli sync
docker compose -f compose.prod.yml down      # -v — удалить и данные
```

Сборке нужен доступ к Docker Hub, ghcr.io, PyPI и npm (см. «Если сборка не идёт»).
`failed to install root certificate` в логах Caddy при `DOMAIN=localhost` — не ошибка: корневая
ФС контейнера только для чтения, и внутренний сертификат не ставится в его хранилище доверия.

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
