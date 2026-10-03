# ADR-0008: `kernel.py` — вторая точка входа любого модуля для domain-типов

- Статус: принято
- Дата: 2026-10-03

## Контекст
ADR-0007 ввёл `shared.kernel` — точку входа без фреймворков, потому что import-linter проверяет
и косвенные импорты, а `shared/public.py` тянет SQLAlchemy и FastAPI. Тогда это решили только
для `shared`.

В P2-04 та же проблема возникла в `tools`. Порт `ToolExecutor` и события хода в
`agent.domain` используют `ToolResult`, `ToolErrorCode` из `tools`. Пока `tools/public.py`
экспортировал только эти dataclass'ы, импорт был чистым. Теперь `public.py` экспортирует ещё и
`ToolRegistry` (structlog, jsonschema), и контракт «domain не зависит от фреймворков, БД и SDK»
ломается транзитивно:

```
app.modules.agent.domain.events -> app.modules.tools.public
app.modules.tools.public -> app.modules.tools.application.registry -> structlog
```

Та же ситуация повторится с `memory` (DialogState в портах agent), `knowledge` и любым модулем,
чьи типы нужны чужому domain-слою.

Вторая сторона той же границы. architecture.md §5 говорит, что порт `ToolExecutor`
принадлежит agent и его реализует Tool Registry. Но направление зависимостей между модулями
(`chat → agent → tools | memory | tenants → …`) запрещает `tools` импортировать `agent`.
Поэтому Registry не может принимать `ToolCall` и `TurnContext` агента.

## Решение
1. **Любой модуль может иметь `app/modules/<module>/kernel.py`.** Это вторая точка входа, наравне
   с `public.py`. В неё входят только реэкспорты domain-типов без фреймворков, БД и SDK:
   value objects, DTO, коды ошибок, исключения домена. Сервисов, репозиториев и реализаций
   портов в ней нет.
   - domain-слои других модулей импортируют чужой `kernel`, а не `public`. Остальные слои могут
     импортировать любую из двух точек; `public.py` по-прежнему реэкспортирует всё, включая
     типы из kernel.
   - Forbidden-контракт «domain и kernel модулей не зависят от фреймворков, БД и SDK»
     применяется к `app.modules.*.domain` и `app.modules.*.kernel` (было `shared.kernel`).
     Если в чей-то kernel попадёт structlog или SQLAlchemy, сломается `make check`.
   - `kernel` уже есть в `exhaustive_ignores` контракта слоёв. Protected-контракты закрывают
     только `domain/application/infrastructure/api`, поэтому `kernel` доступен извне без
     правок конфигурации.
   - Первый случай после `shared` — `tools.kernel`: `ToolResult`, `ToolError`, `ToolErrorCode`,
     `ToolContext`, `ToolInvocation`.
2. **Порт верхнего модуля, реализованный нижним, подключается адаптером в верхнем модуле.**
   У `tools` свои входные типы (`ToolInvocation`, `ToolContext`, `ToolDefinition`). Протокол
   `ToolExecutor` реализует `RegistryToolExecutor` в `agent.infrastructure`: он переводит
   `ToolCall` → `ToolInvocation`, `TurnContext` → `ToolContext`, `ToolDefinition` →
   `ToolSchema` и делегирует в `ToolRegistry`. Композиция (какой реестр отдать агенту) — у того,
   кто собирает агент (chat, P2-08).

## Альтернативы
- **Убрать structlog из реестра**: логи исполнения инструментов пишет адаптер в agent, а
  `tools/public.py` остаётся «чистым». Проблема исчезает только до следующего инфраструктурного
  экспорта: HTTP-инструменты тенанта (httpx), инструменты знаний (Qdrant). Кроме того, логика
  про исполнение инструментов (длительность, таймаут, сбой обработчика) оказывается вне модуля,
  который её знает.
- **Перенести `ToolResult` и `ToolError` в `shared`**: `shared` становится свалкой чужих
  предметных типов, граница `tools` размывается. Проблема повторится для следующего модуля.
- **Разрешить domain импортировать `public` и снять транзитивную проверку**
  (`allow_indirect_imports`). Тогда контракт перестаёт ловить реальные утечки: domain может
  через `public` дотянуться до SQLAlchemy, и линтер этого не увидит.

## Последствия
- Уточняет ADR-0001 («единственная точка входа `public.py`»): точек входа две, и у второй
  узкое назначение. Обобщает решение 1 из ADR-0007; ADR-0007 остаётся в силе (`shared.kernel`
  теперь частный случай), его часть о ключах виджета не затронута.
- domain-типы, нужные чужим domain-слоям, сразу кладём в kernel модуля. Новый kernel не
  требует правок import-linter: его подхватывает шаблон `app.modules.*.kernel`.
- При выносе модуля в сервис (ADR-0001) kernel становится пакетом общих DTO, а `public.py`
  заменяется клиентом.
- Обратные порты (верхний модуль объявляет, нижний реализует) всегда требуют адаптера в
  верхнем модуле. Это небольшой шаблонный код, зато направление зависимостей не нарушается.
- architecture.md §5 уже уточнён в P2-04: порт `ToolExecutor` реализуется через
  `RegistryToolExecutor`.
