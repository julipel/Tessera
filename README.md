# Стартовый набор: платформа AI-консультанта + Claude Code

## Что внутри

```
CLAUDE.md                     правила для Claude Code (читается каждую сессию)
docs/architecture.md          архитектура: модули, агентный цикл, знания, данные
docs/contracts.md             SSE-протокол, UI-компоненты, инструменты, AgentConfig
docs/roadmap.md               этапы P0–P9, задачи под отдельные сессии
docs/adr/                     принятые решения + шаблон
.claude/settings.json         разрешения и хуки
.claude/hooks/                защита generated/.env/lock, автоформат, make check-fast на Stop
.claude/commands/             /task, /adr, /eval, /review
.claude/agents/               reviewer, test-writer, frontend, prompt-engineer
.mcp.json                     Context7 (документация библиотек), Playwright (проверка UI)
Makefile                      каркас команд (оживает в P1)
evals/                        формат эталонных диалогов + пример
packages/contracts/schemas/   место для JSON Schema контрактов
```

## Как начать

1. Требования (WSL/Ubuntu): `git`, `python3`, `make`, Docker, Node.js + pnpm, uv.
2. Распакуйте архив в пустую папку проекта (в файловой системе Linux, например `~/projects/`),
   затем `git init && git add . && git commit -m "Стартовый набор"`.
3. Запустите `claude` в корне. При первом запуске подтвердите MCP-серверы из `.mcp.json`
   и доверие к хукам проекта.
4. Пройдите P0 (выбор пилота, эталонные диалоги, JSON Schema). Здесь решения за вами,
   Claude Code — помощник.
5. Дальше по одной задаче: `/task P1-01`, проверить план, подтвердить, `/review`, коммит, `/clear`.

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
