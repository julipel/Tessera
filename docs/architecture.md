# Архитектура

## 1. Цель и принципы

Платформа, в которой AI-агент становится консультантом по конкретному бизнесу: получает
знания из подключённых источников, ведёт многошаговый диалог, уточняет, ищет, вызывает API
и показывает в чате интерактивные элементы.

Принципы:
1. **Единое ядро, конфигурируемая специфика.** Новая компания = новый тенант с конфигурацией
   и данными, без изменения кода ядра.
2. **Агент как цикл с инструментами.** Не конвейер «поиск → ответ». Поиск по знаниям,
   каталогу, API компании, показ карточек — всё это инструменты; модель решает, что делать.
3. **Структурированное — структурно.** Каталог, цены, наличие — фильтры и SQL. Векторы — для текстов.
4. **Модель не генерирует факты для UI.** Карточки, цены, ссылки берутся из данных бэкендом.
5. **Контракты — источник истины** для связи бэкенда и фронтенда.
6. **Наблюдаемость с первого дня.** Каждый шаг агента трассируется.

## 2. Модули

| Модуль | Ответственность |
|---|---|
| `tenants` | Тенанты, AgentConfig (версии), брендинг, API-ключи виджета, публичный конфиг |
| `chat` | Диалоги, сообщения, HTTP/SSE API, приём действий кнопок, сборка стрима событий |
| `agent` | Агентный цикл, сборка промпта, LLM-адаптеры, лимиты, fallback |
| `tools` | Реестр инструментов, валидация аргументов, исполнение, HTTP-инструменты тенанта |
| `knowledge` | Источники, коннекторы, ingestion, чанкинг, индексация, гибридный поиск, каталог |
| `memory` | Состояние диалога (слоты, показанные сущности), суммаризация истории |
| `observability` | AgentEvent-лог, трейсы, метрики, стоимость |
| `shared` | Базовые типы, ошибки, tenant context, утилиты БД (без бизнес-логики) |

Зависимости модулей (только через `public.py`):

```
chat ──► agent ──► tools ──► knowledge
  │        │  └──► memory
  │        └─────► tenants
  └──────────────► tenants
все ─────────────► observability, shared
```

`knowledge` и `tools` не знают про `chat`. `agent` не знает про HTTP.

## 3. Слои внутри модуля

```
modules/<module>/
├── domain/          # сущности, value objects, доменные ошибки, порты (Protocol)
├── application/     # use cases, сервисы, DTO
├── infrastructure/  # SQLAlchemy-модели и репозитории, клиенты внешних API, адаптеры
├── api/             # FastAPI роутеры, схемы запросов/ответов
└── public.py        # единственная точка входа для других модулей
```

Правило зависимостей: `api → application → domain`, `infrastructure → domain`.
`domain` не импортирует ничего из фреймворков.
`api` может импортировать `infrastructure` — для сборки зависимостей через FastAPI dependencies
(репозиторий/адаптер → use case). `application` и `infrastructure` друг о друге не знают.
Правила проверяет import-linter (контракты в `apps/api/pyproject.toml`).

## 4. Поток обработки сообщения

```
Browser ─POST /v1/conversations/{id}/messages (Accept: text/event-stream)─► chat.api
  chat.application.HandleUserMessage
    1. загрузить Conversation, AgentConfig (активная версия) тенанта
    2. сохранить Message(user)
    3. agent.run_turn(ctx) → AsyncIterator[AgentEvent]
    4. каждое событие → SSE-событие по docs/contracts.md
    5. сохранить Message(assistant), ToolCall-и, обновлённое состояние диалога
  ◄─ SSE: turn_started, text_delta..., tool_started, tool_result, component, ..., done
```

Отмена: закрытие SSE-соединения клиентом отменяет задачу хода (`asyncio.CancelledError`),
частичный ответ сохраняется со статусом `interrupted`. Явная отмена
(`POST .../turns/{turn_id}/cancel`) прерывает агента (он работает в отдельной задаче, события
идут в стрим через очередь), стрим дописывает накопленное и заканчивается `done{interrupted}`.
Реестр ходов — в памяти процесса; межпроцессная отмена (Redis) — P7.

## 5. Агентный цикл

```python
async def run_turn(ctx: TurnContext) -> AsyncIterator[AgentEvent]:
    messages = build_context(ctx)            # промпт + история + состояние диалога
    for step in range(ctx.config.limits.max_steps):
        async for chunk in llm.stream(LLMRequest(messages, tools=ctx.tools.schemas())):
            match chunk:                     # клиент без состояния:
                case ResponseCompleted(response): pass   # финальный ответ — последний чанк
                case _: yield from translate(chunk)      # text_delta, tool_call_started
        if not response.tool_calls:
            yield TurnCompleted(...)
            return
        results = await ctx.tools.execute_many(response.tool_calls, ctx)  # параллельно, с таймаутами
        for r in results:
            yield ToolResultEvent(r)
            for component in r.components:   # UI-компоненты из инструментов
                yield ComponentEvent(component)
        messages += [response.as_message(), *[r.as_message() for r in results]]
        ctx.state.apply(results)             # обновление состояния диалога
    yield StepLimitReached(...)              # мягкое завершение с сообщением пользователю
```

Правила:
- Невалидные аргументы инструмента не ломают ход: модель получает ошибку валидации и может
  исправиться. После `limits.max_tool_retries` — финальный ответ с извинением.
- Лимиты: `max_steps`, `max_tool_calls_per_step`, таймаут инструмента, таймаут хода,
  бюджет токенов на ход.
- LLM-провайдер за портом `LLMClient` (domain). Реализации: OpenAI, Anthropic, `FakeLLM` для тестов.
- Fallback: на таймаут/5xx/429 — ретрай с backoff, затем резервная модель из конфига.

## 6. Сборка системного промпта

Слои (порядок фиксирован, каждый версионируется):

1. **Platform** — общие правила агента: честность, работа с инструментами, политика уточнений,
   формат ответа, запрет выдумывать данные. Живёт в коде ядра.
2. **Tenant** — роль, тон, о компании, ограничения, что можно/нельзя обещать. Из AgentConfig.
3. **Scenario** — инструкции активного сценария (подбор товара, поддержка, запись). Из AgentConfig.
4. **Runtime context** — дата/время, язык, канал, состояние диалога (слоты), краткая сводка
   ранней истории. Генерируется на каждом ходе.

Политика уточнений (в Platform-слое): спрашивать, если не хватает критичного параметра или
запрос неоднозначен так, что ответы сильно различаются; не больше одного вопроса за раз;
если можно дать полезный ответ сразу — ответить и уточнить попутно; не переспрашивать то,
что есть в состоянии диалога.

## 7. Состояние диалога и память

- **История** — сообщения и tool-вызовы в БД. В контекст модели идут последние N ходов целиком.
- **Сводка** — при превышении порога токенов старая часть истории суммаризуется (фоновой задачей
  после хода), сводка кладётся в Runtime context.
- **Состояние (DialogState)** — JSON: слоты (заданы схемой сценария в AgentConfig, напр. budget,
  size, purpose), `shown_entities` (id показанных товаров), `facts` (что пользователь сообщил),
  `active_scenario`. Обновляется инструментом `update_dialog_state` и результатами инструментов.
  Всегда передаётся модели целиком — поэтому агент не переспрашивает.

## 8. Знания

### Коннекторы
Общий интерфейс:
```python
class SourceConnector(Protocol):
    kind: str
    async def discover(self, cfg) -> list[RawItemRef]: ...
    async def fetch(self, ref) -> RawItem: ...
    async def changed_since(self, cfg, cursor) -> list[RawItemRef]: ...  # инкрементально
```
Реализации: `website` (краулер + sitemap), `file` (PDF/DOCX/MD/TXT), `table` (CSV/XLSX),
`http_api` (декларативный маппинг), `database` (SQL-запрос из конфига, read-only).

### Нормализация
Каждый RawItem превращается либо в **Document** (текст + метаданные), либо в **Entity**
(структурированная запись: товар, услуга, филиал) по маппингу из конфига источника.
Entity хранится в Postgres: нормализованные поля (`title`, `price`, `currency`, `in_stock`,
`category`, `url`, `image_url`) + `attributes JSONB`. Текстовое описание Entity также
индексируется как Document со ссылкой на entity_id.

### Индексация и поиск
- Чанкинг по структуре документа (заголовки), 300–800 токенов, overlap, метаданные:
  tenant_id, source_id, url, title, section, updated_at.
- Qdrant: коллекция на окружение, изоляция по payload `tenant_id` (обязательный фильтр в
  репозитории). Dense + sparse (BM25) векторы, гибридный поиск с RRF.
- Реранкинг top-k (по конфигу; можно отключить).
- `search_knowledge` возвращает фрагменты с источниками; `search_catalog` — Entity по
  фильтрам + опционально семантическому запросу по описанию.
- Ingestion — фоновые задачи arq, статусы в `SourceSync`, повторная синхронизация по расписанию.

## 9. Инструменты

- `ToolDefinition`: имя, описание для модели, JSON Schema аргументов, исполнитель, таймаут,
  флаги (`side_effect`, `requires_confirmation`).
- Встроенные инструменты — в коде ядра, включаются тенанту через AgentConfig.
- HTTP-инструменты тенанта — декларативно (метод, URL-шаблон, auth из секретов, маппинг ответа,
  опциональный маппинг в UI-компонент). Кода на тенанта не пишем.
- Результат инструмента: `content` (для модели, компактный), `components` (для UI),
  `state_patch` (для DialogState), `error`.
- Инструменты с побочными эффектами (создать заявку, бронь) требуют подтверждения
  пользователя через компонент `confirm`.

## 10. Мультитенантность

- `TenantContext` определяется по ключу виджета (публичный) или по токену админки.
- Все репозитории принимают `tenant_id` явно; в тестах есть проверка изоляции.
- AgentConfig версионируется: `draft` → `active`. Диалог запоминает версию конфига,
  с которой начался (для воспроизводимости и разбора).
- Брендинг — дизайн-токены в конфиге (цвета, радиусы, шрифт, логотип, имя ассистента,
  приветствие, стартовые подсказки), отдаются через публичный эндпоинт.

## 11. Модель данных (основа)

```
Tenant(id, slug, name, status)
AgentConfig(id, tenant_id, version, status, config JSONB, created_at)
WidgetKey(id, tenant_id, key_hash, allowed_origins[])
Source(id, tenant_id, kind, config JSONB, schedule, status)
SourceSync(id, source_id, started_at, finished_at, status, stats JSONB, cursor)
Document(id, tenant_id, source_id, external_id, title, url, content_hash, metadata JSONB)
Chunk(id, document_id, tenant_id, ord, text, token_count)  # вектор — в Qdrant, id совпадает
Entity(id, tenant_id, source_id, external_id, type, title, price, currency, in_stock,
       category, url, image_url, attributes JSONB, updated_at)
Conversation(id, tenant_id, agent_config_id, channel, visitor_id, state JSONB, summary, created_at)
Message(id, conversation_id, tenant_id, role, status, content, input JSONB, blocks JSONB,
        client_message_id, created_at)  # input — UserInput (user); blocks — текст/компоненты
                                        # в порядке стрима (assistant); content — плоский текст
ToolCall(id, message_id, tenant_id, name, arguments JSONB, result JSONB, error, duration_ms)
AgentEvent(id, tenant_id, conversation_id, turn_id, trace_id, type, payload JSONB, ts)
```

## 12. Надёжность

| Ситуация | Поведение |
|---|---|
| Таймаут / 5xx / 429 LLM | ретрай с backoff → резервная модель → `error` с `retryable: true` |
| Обрыв стрима у клиента | ход отменяется, частичный ответ сохраняется `interrupted` |
| Невалидные аргументы инструмента | ошибка возвращается модели, до N попыток |
| Ошибка внешнего API тенанта | структурированная ошибка модели → «данные временно недоступны» |
| Лимит шагов | мягкое завершение с понятным сообщением |
| Пустой/битый ответ модели | один повтор, затем fallback-фраза из конфига тенанта |

## 13. Наблюдаемость

- Каждое событие хода пишется в `AgentEvent` (trace_id на ход).
- Langfuse: трейсы LLM-вызовов, версия промпта, токены, стоимость.
- structlog + OpenTelemetry для HTTP/БД. Sentry для исключений.
- Метрики: латентность до первого токена, длительность хода, шаги на ход, ошибки инструментов,
  стоимость на диалог.

## 14. Frontend

- `apps/web`: Next.js. Страницы: чат (полноэкранный), демо-страница виджета, админка (позже).
- Встраиваемый виджет: `widget.js` → iframe с чатом, конфиг по ключу виджета, тема из токенов.
- Рендер сообщения — список блоков: текст (markdown, стримится) + компоненты из
  discriminated union `type` (см. contracts). Неизвестный тип игнорируется.
- Клиент SSE с переподключением, состояниями «печатает / ищет / ошибка / повторить».
- Типы — только из `@/contracts` (`apps/web/src/contracts/generated`, см. contracts.md).

## 15. Путь масштабирования

Модульный монолит → вынос `knowledge` ingestion в отдельный воркер-сервис (уже на arq) →
при необходимости `agent` как отдельный сервис. Каналы (Telegram, WhatsApp) добавляются
как адаптеры к `chat.application`, не меняя агент.
