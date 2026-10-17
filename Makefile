# Каркас команд. Цели становятся рабочими по мере выполнения задач P1-xx.
# Проверки пропускают части проекта, которых ещё нет, чтобы Stop-хук не блокировал ранний этап.
API := apps/api
WEB := apps/web
HAS_API := $(wildcard $(API)/pyproject.toml)
HAS_WEB := $(wildcard $(WEB)/package.json)

.PHONY: up down ps install check check-fast test lint-py typecheck-py arch lint-web typecheck-web test-web \
        contracts contracts-check migrate migration eval eval-diff loadtest demo-public seed sync retention pilot admin-user dev-api dev-web worker \
        deploy-build deploy-up deploy-seed deploy-sync deploy-retention deploy-admin-user deploy-ps deploy-logs deploy-db-roles deploy-backup

# --wait: команда завершается, когда все сервисы прошли healthcheck
up:
	docker compose up -d --wait

down:
	docker compose down

ps:
	docker compose ps

install:
	cd $(API) && uv sync --locked
	pnpm install --frozen-lockfile

check-fast: lint-py typecheck-py arch

check: check-fast contracts-check test lint-web typecheck-web test-web

lint-py:
ifneq ($(HAS_API),)
	cd $(API) && uv run ruff check . && uv run ruff format --check .
endif

typecheck-py:
ifneq ($(HAS_API),)
	cd $(API) && uv run mypy
endif

arch:
ifneq ($(HAS_API),)
	cd $(API) && uv run lint-imports
endif

test:
ifneq ($(HAS_API),)
	cd $(API) && uv run pytest -q -m "not live"
endif

lint-web:
ifneq ($(HAS_WEB),)
	cd $(WEB) && pnpm exec eslint .
endif

typecheck-web:
ifneq ($(HAS_WEB),)
	cd $(WEB) && pnpm run typecheck
endif

test-web:
ifneq ($(HAS_WEB),)
	cd $(WEB) && pnpm run --if-present test
endif

# JSON Schema → Pydantic (apps/api) и TS (apps/web), ADR-0005
contracts:
	scripts/contracts.sh generate

contracts-check:
	scripts/contracts.sh check

migrate:
	cd $(API) && uv run alembic upgrade head

migration:
	cd $(API) && uv run alembic revision --autogenerate -m "$(m)"

seed:
	cd $(API) && uv run python -m app.cli seed

# Пользователь админки (ADR-0036): make admin-user email=a@b.c [args="--tenant demo-beauty=editor"].
# Пароль нового пользователя — ADMIN_PASSWORD или ввод; см. --help у `python -m app.cli admin-user`.
admin-user:
	cd $(API) && uv run python -m app.cli admin-user $(email) $(args)

# Синхронизация источников тенантов в этом процессе, без воркера (ADR-0019).
# make sync [t="demo-beauty"] [s="kb"] — тенант / источник; по умолчанию все из config/tenants.
sync:
	cd $(API) && uv run python -m app.cli sync $(t) $(if $(s),--source $(s),)

# Чистка данных посетителей по срокам хранения (ADR-0043) — то же, что ежедневный cron воркера.
# make retention [t="demo-beauty"] [dry=1] — dry=1 только считает (удаление откатывается).
retention:
	cd $(API) && uv run python -m app.cli retention $(t) $(if $(dry),--dry-run,)

# Тенанты, источники и их индексация с нуля: после make up.
pilot: migrate seed sync

dev-api:
	cd $(API) && uv run uvicorn --factory app.main:create_app --reload

dev-web:
	cd $(WEB) && pnpm dev

worker:
	cd $(API) && uv run arq app.worker.WorkerSettings

# Судья эвалов — другая модель, чем агент тенанта: модель не оценивает сама себя.
# Переопределить: make eval EVAL_JUDGE_MODEL=... EVAL_JUDGE_PROVIDER=...
EVAL_JUDGE_PROVIDER ?= openai
EVAL_JUDGE_MODEL ?= gpt-6.1-sol

eval:
	cd $(API) && uv run python -m evals.run $(if $(f),--filter $(f),) \
		--judge-provider $(EVAL_JUDGE_PROVIDER) --judge-model $(EVAL_JUDGE_MODEL)

# Без аргументов — два последних отчёта; иначе make eval-diff a=<старый> b=<новый>
# (имя отчёта без .json или путь от корня репозитория). Код 1 — есть ухудшения.
eval-diff:
	cd $(API) && uv run python -m evals.diff $(a) $(b)

# Нагрузочный тест (P7-05): n диалогов по turns ходов, одновременно c (по умолчанию = n).
# Свой стенд на моке модели — см. scripts/loadtest.sh (там же прогон против живого API).
loadtest:
	scripts/loadtest.sh $(if $(n),--dialogs $(n),) $(if $(c),--concurrency $(c),) \
		$(if $(turns),--turns $(turns),)

# Публичное демо через туннель cloudflared (P9-00b): витрины по временной ссылке, Ctrl+C —
# остановить. Запуск, сценарии и ограничения — docs/demo.md.
demo-public:
	scripts/demo-public.sh

# Боевой стек на сервере (P9-01, ADR-0040, docs/deploy.md): настройки — deploy/.env.
# deploy-up собирает образы и поднимает стек; миграции идут сами (сервис migrate) до api/worker.
DEPLOY := docker compose -f deploy/compose.prod.yml --env-file deploy/.env

deploy-build:
	$(DEPLOY) build

deploy-up:
	$(DEPLOY) up -d --build --wait

# Тенанты из config/tenants, вшитого в образ API (после git pull — deploy-up, затем deploy-seed).
deploy-seed:
	$(DEPLOY) run --rm api python -m app.cli seed

# make deploy-sync [t="demo-beauty"] [s="kb"] — как make sync, внутри контейнера.
deploy-sync:
	$(DEPLOY) run --rm api python -m app.cli sync $(t) $(if $(s),--source $(s),)

deploy-retention:
	$(DEPLOY) run --rm api python -m app.cli retention $(t) $(if $(dry),--dry-run,)

# make deploy-admin-user email=a@b.c [args="--tenant demo-beauty=viewer"] — пароль вводится.
deploy-admin-user:
	$(DEPLOY) run --rm api python -m app.cli admin-user $(email) $(args)

deploy-ps:
	$(DEPLOY) ps

# make deploy-logs [svc=api]
deploy-logs:
	$(DEPLOY) logs -f --tail 200 $(svc)

# Роль приложения в Postgres (D-3): для тома, созданного до неё, и после смены APP_DB_PASSWORD.
# Новый том получает её сам (docker-entrypoint-initdb.d). up пересоздаёт postgres, если
# изменились deploy/.env или compose: exec видит окружение контейнера с момента создания.
deploy-db-roles:
	$(DEPLOY) up -d --wait postgres
	$(DEPLOY) exec -T postgres sh /docker-entrypoint-initdb.d/10-app-role.sh

# Дамп Postgres в backups/<время>.sql.gz на хосте (вне git).
deploy-backup:
	mkdir -p backups
	$(DEPLOY) exec -T postgres sh -c 'pg_dump -U "$$POSTGRES_USER" "$$POSTGRES_DB"' \
		| gzip > backups/$$(date +%Y%m%d-%H%M%S).sql.gz
