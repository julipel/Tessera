# Каркас команд. Цели становятся рабочими по мере выполнения задач P1-xx.
# Проверки пропускают части проекта, которых ещё нет, чтобы Stop-хук не блокировал ранний этап.
API := apps/api
WEB := apps/web
HAS_API := $(wildcard $(API)/pyproject.toml)
HAS_WEB := $(wildcard $(WEB)/package.json)

.PHONY: up down ps install check check-fast test lint-py typecheck-py arch lint-web typecheck-web test-web \
        contracts contracts-check migrate migration eval eval-diff seed dev-api dev-web worker

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

dev-api:
	cd $(API) && uv run uvicorn app.main:app --reload

dev-web:
	cd $(WEB) && pnpm dev

worker:
	cd $(API) && uv run arq app.worker.WorkerSettings

# Реализуется в P3-01 / P3-02
eval:
	cd $(API) && uv run python -m evals.run $(if $(f),--filter $(f),)

eval-diff:
	cd $(API) && uv run python -m evals.diff
