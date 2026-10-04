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
  исправиться. Аргументы, не разобранные как JSON-объект, отклоняет сам цикл, не вызывая
  инструмент. Шаги с `validation_error` считаются за ход; после `limits.max_tool_retries`
  — мягкое завершение.
- Мягкое завершение (лимит шагов, исчерпаны ретраи) — детерминированное: текст
  `assistant.fallback_message` как обычный ответ и `TurnCompleted(finish=step_limit |
  tool_retries_exhausted)`, без дополнительного вызова модели.
- Порт `ToolExecutor` принадлежит agent; его реализует Tool Registry (`tools`) через адаптер
  `RegistryToolExecutor` в agent — зависимости идут agent → tools. Registry: валидация по JSON
  Schema, параллельность, таймауты, ошибки исполнения — в `ToolResult.error`. Таймаут
  инструмента в ретраи не входит.
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
  `active_scenario`. Обновляется инструментом `update_dialog_state` и результатами инструментов
  (`state_patch`, формат — contracts.md §4). Всегда передаётся модели целиком (Runtime-слой
  промпта) — поэтому агент не переспрашивает. Тип и слияние — `memory` (`memory.kernel`,
  ADR-0008); патчи применяет агентный цикл; хранится в `Conversation.state`, chat записывает
  последнее состояние хода вместе с ответом (в т.ч. `interrupted`).

## 8. Знания

### Коннекторы
Общий интерфейс:
```python
class SourceConnector(Protocol):  # knowledge/domain/ports.py
    kind: SourceKind
    async def discover(self, source: SourceSpec) -> Listing: ...               # полный список + курсор
    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem: ...  # DocumentItem | EntityItem
    async def changed_since(self, source: SourceSpec, cursor) -> Listing: ...  # инкрементально
```
`SourceSpec = (tenant_id, source_id, config)` — коннектор знает владельца источника (ADR-0012).
`Listing = (refs, cursor)`: курсор для следующего `changed_since`. Коннектор отдаёт уже
нормализованный элемент (по маппингу своего конфига); остальное делает общий пайплайн
`run_sync` (knowledge/application/ingestion.py):
- `content_hash` — sha256 канонического JSON элемента, считает пайплайн, не коннектор.
  Совпал с сохранённым — элемент не перезаписывается; иначе upsert по `external_id`,
  чанки документа заменяются целиком.
- Есть курсор последней успешной синхронизации — `changed_since`, иначе (или `full`) —
  `discover`. Только полный discover удаляет элементы, пропавшие из источника (кроме тех,
  что не удалось получить в этом прогоне).
- Каждый элемент коммитится отдельно; ошибка элемента считается в `stats.failed`,
  синхронизация продолжается. Ошибка листинга или нет коннектора — `SourceSync.failed`.
Реализации: `website` (краулер + sitemap), `file` (PDF/DOCX/MD/TXT; файлы в
`<KNOWLEDGE_FILES_DIR>/<tenant_id>/<source_id>/`, приводятся к markdown: DOCX — заголовки
по стилям `Heading N`/`Title`, списки, таблицы; PDF — текст постранично, без заголовков
и OCR; битый/зашифрованный/пустой файл — ошибка элемента), `table` (CSV/XLSX → Entity, см. ниже),
`http_api` (декларативный маппинг), `database` (SQL-запрос из конфига, read-only).

Коннектор `table`: все `*.csv`/`*.xlsx` каталога источника (как у `file`), первая строка —
заголовок, строка — Entity, `external_id` — значение колонки id (уникально в источнике).
```yaml
config:
  entity_type: product
  sheet: "Каталог"            # XLSX, по умолчанию первый лист
  delimiter: ";"              # CSV, по умолчанию — самый частый из , ; | \t в заголовке
  currency: RUB               # если колонки валюты нет или ячейка пустая
  columns: { external_id: "Артикул", title: "Название", price: "Цена", in_stock: "Наличие",
             currency: ..., category: ..., url: ..., image_url: ... }   # обязательны первые два
  attributes: { color: "Цвет", volume_ml: { column: "Объём, мл", type: number } }  # string|number|boolean
```
Цена: `1 990,50 р.`/`1,990.50` → Decimal, текст без цифр («по запросу») — нет цены.
Наличие: да/нет, true/false, +/-, «в наличии»…, число — остаток (> 0). Неверный конфиг,
нечитаемый файл или нет колонки из маппинга — ошибка синхронизации (иначе полный discover
удалил бы сущности файла). Пустой или повторяющийся id, пустое название, неразбираемые
цена/наличие/валюта — ошибка элемента. Курсор — `mtime_ns`, как у `file`; `changed_since`
отдаёт строки изменённых файлов.

Коннектор `website` (P4-05c — краулер; сеть — P4-05b; разбор — P4-05a): страница — документ, `external_id`
и `url` — нормализованный URL (`web_urls`: схема и хост в нижнем регистре, IDN → punycode,
без порта по умолчанию и fragment; путь и query не меняются). HTML → markdown
(`html_extract`, stdlib `html.parser`): корень контента — первый непустой `main`/`[role=main]`,
иначе единственный `article`, иначе `body`; без `nav`/`aside`/`footer`/скриптов/форм ввода/
скрытых элементов, `header` — только при корне `body`. Таблица из одной строки или колонки
и таблица-вёрстка выводятся блоками. Ссылки для обхода — со всей страницы (с учётом
`<base>`, `rel=nofollow`, `meta robots`). Sitemap (`sitemap`): `urlset`, `sitemapindex`,
текстовый список, gzip; DTD отклоняется, лимиты протокола — 50 МБ и 50 000 записей.
Сеть (P4-05b, ADR-0013): `WebClient` (`web_client`) поверх `GuardedTransport` (`web_guard`) —
подключения только к публичным IP на портах 80/443, проверка в сетевом бэкенде httpcore
(защищает и от DNS rebinding), без прокси из окружения; редиректы вручную (≤ 5, только на
разрешённые хосты), лимит тела после распаковки, ожидаемый Content-Type, общий таймаут.
robots.txt (`robots`, RFC 9309 с `*`/`$`, `Crawl-delay`, `Sitemap`): 4xx — без ограничений,
5xx и сбой сети — ошибка синхронизации.
Обход (`website_connector`, P4-05c):
```yaml
config:
  start_urls: ["https://example.ru/"]  # 1–20; их хосты — единственные разрешённые
  sitemaps: []           # явные; ещё Sitemap: из robots, без обоих — пробуется /sitemap.xml
  follow_links: true
  max_pages: 500         # потолок платформы 5000; обрывает обход (порядок детерминирован)
  max_depth: 5           # по ссылкам от стартовых и sitemap-страниц
  include: []            # префиксы пути; стартовые URL фильтры не отсекают
  exclude: []
  crawl_delay_s: 0.5     # итог — max(конфиг, Crawl-delay robots ≤ 10 с)
  max_duration_s: 1800
```
Обход в ширину, последовательный. Документ — 2xx HTML без `noindex` с непустым текстом,
ключ — URL после редиректов. 4xx (кроме 429), `noindex`, не-HTML, слишком большая страница,
редирект за пределы сайта, запрет robots — страницы нет, её документ удалится. 429/5xx/сеть —
URL остаётся в листинге без кэша: `fetch` повторит запрос, при неудаче — ошибка элемента
(не удаляется). Ошибка синхронизации: неверный конфиг, недоступен robots.txt, стартовая
страница недоступна или запрещена, явный sitemap недоступен, вышло `max_duration_s`.
Курсора нет — каждая синхронизация полная; страницы обхода кэшируются для `fetch`.

### Нормализация
Каждый RawItem превращается либо в **Document** (текст + метаданные), либо в **Entity**
(структурированная запись: товар, услуга, филиал) по маппингу из конфига источника.
Entity хранится в Postgres: нормализованные поля (`title`, `price`, `currency`, `in_stock`,
`category`, `url`, `image_url`) + `attributes JSONB`. Текстовое описание Entity также
индексируется как Document со ссылкой на entity_id.

### Индексация и поиск
- Чанкинг по структуре документа (заголовки), 300–800 токенов, overlap, метаданные:
  tenant_id, source_id, url, title, section, updated_at. `MarkdownChunker`: коннекторы отдают
  markdown, чанк не пересекает секцию, `section` — путь заголовков («Доставка > Сроки»).
- Qdrant: коллекция на окружение, изоляция по payload `tenant_id` (обязательный фильтр в
  репозитории). Dense + sparse (BM25) векторы, гибридный поиск с RRF.
- Реранкинг top-k (по конфигу; можно отключить).
- `search_knowledge` возвращает фрагменты с источниками; `search_catalog` — Entity по
  фильтрам + опционально семантическому запросу по описанию.
- Ingestion — фоновые задачи arq (`app/worker.py`, задача `sync_source`), статусы в `SourceSync`,
  повторная синхронизация по расписанию. Запуск — `request_sync` через порт `SyncQueue`.
  Параллельный запуск одного источника исключён: частичный уникальный индекс допускает одну
  активную (pending/running) синхронизацию на источник, повторный запрос возвращает её и ставит
  в очередь заново; `_job_id = sync:<id>` не даёт arq выполнить одну синхронизацию дважды.

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
Source(id, tenant_id, kind, config JSONB, schedule, status, created_at)
SourceSync(id, tenant_id, source_id, started_at, finished_at, status, stats JSONB, cursor, error)
Document(id, tenant_id, source_id, entity_id?, external_id, title, url, content_hash,
         metadata JSONB, updated_at)  # entity_id — документ-описание Entity (§8)
Chunk(id, document_id, tenant_id, ord, section, text, token_count)  # вектор — в Qdrant, id совпадает
Entity(id, tenant_id, source_id, external_id, type, title, price, currency, in_stock,
       category, url, image_url, attributes JSONB, content_hash, updated_at)
# external_id уникален в пределах source_id (Document, Entity); удаление Source/Entity/Document
# каскадно удаляет зависимые строки.
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
