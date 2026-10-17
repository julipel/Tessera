# Разработка Tessera с Claude Code

## Что внутри

```
CLAUDE.md                     правила для Claude Code (читается каждую сессию)
docs/architecture.md          архитектура: модули, агентный цикл, знания, данные
docs/contracts.md             SSE-протокол, UI-компоненты, инструменты, AgentConfig
docs/roadmap.md               этапы P0–P9, задачи под отдельные сессии
docs/pilot.md                 пилот: магазин косметики и парфюмерии (demo-beauty)
docs/demo.md                  публичное демо: витрины, туннель, сценарии показа
docs/deploy.md                выкладка на один сервер (docker compose, Caddy)
docs/adr/                     принятые решения + шаблон
.claude/settings.json         разрешения и хуки
.claude/hooks/                защита generated/.env/lock, автоформат, make check-fast на Stop
.claude/commands/             /task, /adr, /eval, /review
.claude/agents/               reviewer, test-writer, frontend, prompt-engineer
.mcp.json                     Context7 (документация библиотек), Playwright (проверка UI)
Makefile                      команды проекта (см. CLAUDE.md)
docker-compose.yml            Postgres, Redis, Qdrant для локальной разработки
apps/api/                     backend: uv-проект, Python 3.12, FastAPI
apps/web/                     frontend: Next.js (App Router), pnpm-workspace в корне
config/tenants/               конфигурация демо-тенантов (demo-beauty, demo-garden)
evals/                        эталонные диалоги и отчёты прогонов (make eval)
packages/contracts/schemas/   JSON Schema контрактов (make contracts)
deploy/                       боевой стек: compose.prod.yml, Caddyfile, роли Postgres
scripts/                      генерация контрактов, публичное демо, нагрузочный тест
loadtest/                     отчёты нагрузочного теста (make loadtest)
```

## Как начать

1. Требования (WSL/Ubuntu): `git`, `make`, Docker, uv, Node.js 24 LTS (`.node-version`),
   pnpm через `corepack enable`. Python 3.12 uv установит сам.
2. Клонируйте репозиторий в файловую систему Linux (например, `~/projects/`):
   ```bash
   git clone https://github.com/julipel/Tessera.git
   cd Tessera
   ```
3. `make install` — зависимости по lock-файлам (Python 3.12 uv скачает сам).
4. `make up` — инфраструктура в Docker (Postgres, Redis, Qdrant).
5. `make check` — все проверки (ruff, mypy, import-linter, pytest, tsc, eslint, Playwright).
6. Запустите `claude` в корне. При первом запуске подтвердите MCP-серверы из `.mcp.json`
   и доверие к хукам проекта.
7. P0 — выбор пилота, эталонные диалоги, JSON Schema (в этом репозитории уже пройден).
   Здесь решения за вами, Claude Code — помощник.
8. Дальше по одной задаче: `/task P1-01`, проверить план, подтвердить, `/review`, коммит, `/clear`.

## Ритм работы

- Одна задача — одна сессия. Между задачами `/clear`.
- Всегда читайте план до реализации (Plan mode или шаг 4 в `/task`).
- Агентный цикл (P2-02, P2-03) читайте построчно — это ядро.
- После P2 запускайте `/eval` регулярно; изменения промптов — только через эвалы.
- Если Claude Code предлагает архитектурное решение, которого нет в docs, — `/adr`.

## Настройка под себя

- Модели и провайдеры — в AgentConfig (docs/contracts.md §5), ядро от них не зависит.
- Если вместо Qdrant выберете pgvector — сначала ADR, затем правка architecture.md §8.
- Stop-хук запускает `make check-fast` только при наличии изменений. Если на каком-то этапе
  он мешает, временно уберите блок `Stop` из `.claude/settings.json`.
