# Платформа AI-консультанта

Мультитенантная платформа: AI-агент получает знания из источников компании и ведёт
многошаговый диалог в web-чате с интерактивными элементами. Ядро едино для всех компаний,
вся бизнес-специфика — в конфигурации тенанта.

Документы (читай нужный раздел перед задачей, не весь файл целиком):
- docs/architecture.md — модули, слои, агентный цикл, данные
- docs/contracts.md — SSE-протокол, UI-компоненты, интерфейс инструментов, AgentConfig
- docs/roadmap.md — задачи по этапам (ID вида P2-03)
- docs/adr/ — принятые архитектурные решения; не противоречь им без нового ADR

## Жёсткие правила
- Модульный монолит: `apps/api/src/app/modules/<module>/{domain,application,infrastructure,api}`.
  Другие модули импортируют только `modules/<module>/public.py`. Проверяет import-linter.
- domain не зависит от FastAPI, SQLAlchemy, SDK провайдеров LLM.
- В ядре нет логики конкретного бизнеса. Специфика — только в AgentConfig / ToolDefinition / данных.
- Каждая бизнес-таблица имеет `tenant_id`. Доступ к данным — только через репозитории,
  которые принимают `tenant_id` явно. Никаких запросов без фильтра по тенанту.
- Контракты меняются только в `packages/contracts/schemas/*.json`, затем `make contracts`.
  Сгенерированные файлы (`**/generated/**`) руками не редактировать.
- Каталог товаров ищется структурно (фильтры/SQL). Векторный поиск — для текстов и описаний.
- UI-компоненты в чат отправляют инструменты бэкенда (данные из БД), а не текст модели.
- Секреты только из env. Не читать и не коммитить `.env`.
- Не обновлять зависимости и не добавлять новые без явного запроса.
- Для API библиотек (OpenAI/Anthropic SDK, Qdrant, FastAPI, Next.js) сверяйся с Context7,
  а не с памятью.

## Рабочий цикл
1. Одна задача из roadmap за сессию. Сначала план — жди подтверждения.
2. Тесты вместе с кодом. Для агентного цикла — сначала тесты на FakeLLM.
3. Перед завершением: `make check` зелёный. Отметь задачу в roadmap.
4. Если задача раздувается (> ~600 строк диффа) — остановись и предложи разбить.
5. Архитектурное решение, которого нет в docs, — предложи ADR (`/adr`), не решай молча.

## Команды
- `make up` / `make down` — Postgres, Redis, Qdrant в Docker
- `make check` — ruff, mypy, import-linter, pytest, tsc, eslint
- `make check-fast` — ruff + mypy + import-linter (без тестов)
- `make test` — pytest
- `make contracts` — генерация Pydantic и TS из JSON Schema
- `make migrate` / `make migration m="..."` — Alembic
- `make eval` — прогон эталонных диалогов (evals/)

## Стек
Python 3.12, uv, FastAPI, SQLAlchemy 2 (async), Alembic, PostgreSQL 16, Redis, Qdrant, arq.
Next.js (App Router) + TypeScript, pnpm, Tailwind, shadcn/ui. Тесты: pytest, Playwright.
Версии зафиксированы в lock-файлах.

## Стиль
- Python: type hints везде, mypy strict, Pydantic v2 для DTO, dataclasses/Pydantic для domain.
- Async везде в I/O. Никаких глобальных синглтонов — DI через FastAPI dependencies / контейнер.
- Логи структурированные (structlog), всегда с tenant_id, conversation_id, trace_id.
- Комментарии и docstrings — на русском или английском, но единообразно в модуле.
