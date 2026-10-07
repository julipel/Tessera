# Roadmap

Одна задача ≈ одна сессия Claude Code (`/task P1-02`). Отмечай `[x]` по завершении.
Формат: цель → критерий готовности (DoD). Порядок внутри этапа важен.

---

## P0. Дизайн и эталон (делаете вы + Claude Code как помощник)

- [x] **P0-01 Пилотный бизнес.** Выбрать компанию/домен для пилота, собрать источники
  (сайт, каталог CSV/XLSX, 1–2 API). DoD: `docs/pilot.md` с описанием и списком источников.
- [x] **P0-02 Эталонные диалоги.** 15–20 диалогов в `evals/dialogs/*.yaml` (формат — evals/README.md):
  простые вопросы, подбор с уточнениями, смена темы, «не знаю», запрос вне компетенции.
  DoD: файлы валидны по схеме, покрыты все сценарии пилота.
- [x] **P0-03 Ревизия контрактов.** Пройти docs/contracts.md, поправить под пилот, перенести в
  JSON Schema `packages/contracts/schemas/`. DoD: схемы для SSE-envelope, событий, компонентов,
  UserInput, ToolDefinition/ToolResult, AgentConfig.

## P1. Каркас

- [x] **P1-01 Монорепо и инфраструктура.** uv-проект apps/api, pnpm apps/web, Docker Compose
  (Postgres, Redis, Qdrant), Makefile по CLAUDE.md, `.env.example`. DoD: `make up` поднимает всё.
- [x] **P1-02 Скелет API.** FastAPI app factory, настройки (pydantic-settings), structlog,
  модули-заглушки со слоями и `public.py`, `/health`. DoD: `make check-fast` зелёный.
- [x] **P1-03 Архитектурные проверки.** import-linter: слои и запрет импорта чужих модулей
  мимо public.py; mypy strict; ruff. DoD: намеренное нарушение ловится (тест-пример в PR, затем убрать).
- [x] **P1-04 БД и миграции.** Async SQLAlchemy, Alembic, базовый репозиторий с обязательным
  tenant_id, фикстуры pytest с транзакцией на тест. DoD: миграция применяется, тест изоляции тенантов.
- [x] **P1-05 Генерация контрактов.** `make contracts` → Pydantic + TS. DoD: сгенерированные
  типы импортируются в api и web, проверка в `make check`, что генерация актуальна.
- [x] **P1-05a Имена типов событий.** Добавить `title` inline-объектам `data` в
  `events.schema.json` (напр. `TextDeltaData`, `DoneData`), `make contracts`, обновить тест
  и README схем. Сделать до P1-08. DoD: в generated нет классов `Data`/`Data1…`, `make check` зелёный.
- [x] **P1-05b Имена типов AgentConfig и tools.** `title` для `SlotDefinition.items` в
  `agent_config.schema.json` (сейчас класс `Items`); убрать пустой корневой `Model(RootModel[Any])`
  из `tools_schema` (корень без типа). Можно вместе с P1-06. DoD: в generated нет `Items`/`Model`,
  тест на безымянные классы покрывает все схемы, `make check` зелёный.
- [x] **P1-06a Tenants: данные и seed.** Tenant, AgentConfig (версии, draft/active/archived),
  WidgetKey, репозитории, загрузка конфига из YAML (seed-команда), `config/tenants/demo-beauty.yaml`.
  DoD: seed демо-тенанта идемпотентен, тесты изоляции и версий.
- [x] **P1-06b Tenants: публичный конфиг.** Контракт `public_config.schema.json`, аутентификация
  по `X-Widget-Key`, `GET /v1/public/config`. DoD: тесты API (401 / 404 / 200).
- [x] **P1-07a Единый формат HTTP-ошибок.** Контракт `http_error.schema.json`
  (`{"error": {code, message, retryable}}`, коды из contracts.md §6 + `unauthorized`/`not_found`)
  вместо дефолтного `{"detail"}`, обработчики в `shared`, 500 с trace_id в логе.
  Тест, что `tenant_id` из `WidgetTenant` попадает в строки логов. DoD: тесты API.
- [x] **P1-07b Chat: диалоги и сообщения.** Conversation, Message, эндпоинты создания и истории,
  идемпотентность по client_message_id (use case + уникальный индекс; API-тест — в P1-08).
  Conversation хранит `agent_config_id` версии, с которой начат (architecture.md §10);
  блоки сообщения `blocks` (порядок как в стриме). DoD: тесты API и изоляции репозиториев.
- [x] **P1-08a SSE-стрим с эхо-агентом.** `POST .../messages` отдаёт события по протоколу от
  заглушки агента (эхо с text_delta), запись ответа, сбой агента → `error` + `done{failed}`,
  повтор client_message_id → 409 `duplicate_message`, обрыв соединения → `interrupted`.
  DoD: интеграционный тест читает стрим и проверяет последовательность событий.
- [x] **P1-08b Явная отмена хода.** `POST .../turns/{turn_id}/cancel`: реестр текущих ходов
  в памяти процесса (создаётся в `create_app`), агент — в отдельной задаче, события через
  очередь; отмена → `done{interrupted}`, частичный ответ в истории. 204 / 404 `not_found`
  (нет хода, завершён, чужой тенант). Межпроцессная отмена (Redis) — P7.
  DoD: интеграционный тест отмены на агенте с «воротами», тест 404.
- [x] **P1-09 Минимальный web-чат.** Next.js страница чата: SSE-клиент, рендер текста со
  стримингом, история, индикатор. DoD: Playwright-тест отправляет сообщение и видит ответ.

## P2. Агент

- [x] **P2-01 LLM-порт и FakeLLM.** `LLMClient` Protocol (stream с текстом и tool calls),
  `FakeLLM` со сценариями. DoD: тесты FakeLLM.
- [x] **P2-02 Тесты агентного цикла.** До реализации: прямой ответ; один инструмент; цепочка;
  параллельные вызовы; невалидные аргументы → исправление; лимит шагов; таймаут инструмента;
  отмена. DoD: тесты написаны и падают.
- [x] **P2-03 Агентный цикл.** Реализация по architecture.md §5 до зелёных тестов P2-02.
- [x] **P2-04 Tool Registry.** ToolDefinition, валидация аргументов по JSON Schema,
  параллельное исполнение с таймаутами, ToolResult с components/state_patch. DoD: тесты.
- [x] **P2-05 Сборка промпта.** Слои Platform/Tenant/Scenario/Runtime, Platform-промпт с политикой
  уточнений. DoD: snapshot-тест собранного промпта для демо-тенанта.
- [x] **P2-06 Адаптер OpenAI.** Streaming + tool calling (сверить с Context7). DoD: интеграционный
  тест (маркер `live`, не в CI по умолчанию).
- [x] **P2-07 Адаптер Anthropic.** То же. DoD: оба адаптера проходят общий контрактный тест.
- [x] **P2-08a Подключение агента к chat.** Замена эхо-заглушки (`LoopTurnAgent` поверх
  `AgentLoop`), маппинг AgentEvent → SSE (блоки текст/компонент, `done.usage`, `LLMError` →
  `llm_unavailable`), `LLMClients` по провайдеру, `agent.kernel`; e2e — через мок
  OpenAI-совместимого API. DoD: диалог в web-чате с реальной моделью без инструментов.
- [x] **P2-08b Сохранение ToolCall.** Таблица `tool_calls` (миграция), в `ToolFinished` —
  аргументы, результат, ошибка, `duration_ms` (в SSE `tool_finished` — замер цикла вместо
  оценки chat от раннего `tool_started`); запись в `save()` вместе с ответом, в т.ч.
  `interrupted`. DoD: тесты записи и изоляции тенантов.
- [x] **P2-09 DialogState.** Хранение, `update_dialog_state`, передача в Runtime-слой.
  DoD: тест — агент не переспрашивает известный слот (FakeLLM + проверка контекста).
- [x] **P2-10 Сэмплинг по ADR-0009.** Долг из ADR-0009, делать сразу после P2-08 (там
  ModelConfig впервые доходит до адаптера):
  - убрать `default: 0.3` у `ModelConfig.temperature` в `agent_config.schema.json`, описать
    поле как подсказку для провайдеров, которые её поддерживают; обновить contracts.md §5;
    `make contracts`;
  - debug-лог в structlog, когда адаптер Anthropic отбрасывает заданную temperature;
  - сверить через Context7 поддержку temperature у reasoning-моделей OpenAI; при необходимости
    научить адаптер OpenAI её отбрасывать.

  DoD: в unit-тестах обоих адаптеров отброшенный параметр не попадает в тело запроса и
  логируется; конфиг без temperature проходит валидацию и не передаёт её провайдеру.
- [x] **P2-11a Порт LLMClient: `provider_items` и `openai_compatible` (ADR-0010).**
  `provider_items` у `LLMResponse`/`AssistantMessage`, цикл возвращает их в следующий шаг
  хода без изменений; `FakeLLM` умеет их отдавать. Chat Completions (`OpenAILLM`) —
  провайдер `openai_compatible`: значение в `ModelConfig.provider` (схема, описание «протокол
  API, а не производитель»; `make contracts`), `LLMClients`, env
  `OPENAI_COMPATIBLE_API_KEY`/`OPENAI_COMPATIBLE_BASE_URL`, `.env.example`.
  DoD: тест цикла — `provider_items` шага доходят до следующего запроса; контрактный тест
  `OpenAILLM` под `openai_compatible` проходит.
- [x] **P2-11b Адаптер OpenAI Responses API (ADR-0010).** `OpenAIResponsesLLM` для
  `provider: openai`: `store: false`, `include: ["reasoning.encrypted_content"]`, стрим
  (текст, ранний `ToolCallStarted`, аргументы, reasoning items из `output_item.done`, usage),
  ошибки → `LLMError`, temperature по ADR-0009. Вернуть `update_dialog_state` в demo-beauty.
  Контекст: Chat Completions + tools для `gpt-6.1-sol` → 400 (`param: reasoning_effort`),
  проверено в P2-09. DoD: общий контрактный тест LLMClient с вызовом инструмента проходит на
  трёх адаптерах; live-тест Responses API с инструментом; живой ход demo-beauty с
  `update_dialog_state` в web-чате.

## P3. Эвалы (рано!)

- [x] **P3-01a Раннер эвалов (ADR-0011).** `make eval`: прогон evals/dialogs через агента
  in-process (`LoopTurnAgent`, конфиг из `config/tenants/*.yaml`), детерминированные проверки
  (`tools_called`/`tools_not_called`, `components`, `state_contains`, `must`/`must_not_contain`),
  подстановка реальных `form_id`/`confirm`; проверки судьи — `skipped`.
  DoD: отчёт в `evals/reports/<timestamp>.md` (+ `.json` для P3-02) и сводка в консоль; тесты на FakeLLM.
- [x] **P3-01b LLM-as-judge.** `clarifies`, `max_questions`, `judge` — один вызов судьи на ход
  через порт `LLMClient`, ответ JSON, ошибки разбора → статус `error`; флаги
  `--judge-provider/--judge-model`. DoD: тесты судьи на FakeLLM, отчёт с пояснениями судьи.
- [x] **P3-02 Сравнение прогонов.** Diff двух отчётов: что улучшилось/ухудшилось. DoD: `make eval-diff`.

## P4. Знания

- [x] **P4-01 Модель данных знаний.** Source, SourceSync, Document, Chunk, Entity, миграции.
- [x] **P4-02a Ingestion-пайплайн.** Интерфейс SourceConnector, статусы синхронизации,
  дедупликация по content_hash. DoD: тест на фейковом коннекторе.
- [x] **P4-02b arq-воркер ingestion.** `app/worker.py`, постановка синхронизации в очередь
  (порт SyncQueue), защита от параллельного запуска одного источника. DoD: тест задачи воркера.
- [x] **P4-03a Коннектор file: MD/TXT.** Структурный чанкинг по заголовкам, каталог
  `<root>/<tenant_id>/<source_id>/`, `SourceSpec` в интерфейсе коннектора (ADR-0012).
- [x] **P4-03b Коннектор file: PDF/DOCX.** Парсеры в markdown (pypdf, python-docx).
- [x] **P4-04 Коннектор table.** CSV/XLSX → Entity по маппингу колонок из конфига.
- [x] **P4-05a Коннектор website: разбор.** HTML → markdown (основной контент, ссылки,
  meta robots), sitemap (`urlset`/`sitemapindex`/текст/gzip), нормализация URL.
- [x] **P4-05b Коннектор website: сеть.** HTTP-клиент на httpx с защитой от SSRF в сетевом
  бэкенде (DNS rebinding, редиректы), лимиты ответа, robots.txt (RFC 9309), ADR-0013.
- [x] **P4-05c Коннектор website: краулер.** `WebsiteConnector`: sitemap + обход ссылок,
  лимиты обхода (страницы, глубина, время, Crawl-delay), кэш discover → fetch, подключение
  в воркере, настройки User-Agent/таймаута.
- [x] **P4-06a Индексация в Qdrant: адаптеры.** Порты `Embedder`/`ChunkIndex`, `OpenAIEmbedder`
  (батчи, проверка размерности), `QdrantChunkIndex` на httpx REST: dense + серверный BM25,
  payload с tenant_id, замена/удаление точек документа. DoD: тесты на реальном Qdrant, изоляция.
- [x] **P4-06b Индексация в Qdrant: пайплайн.** Индексация чанков в `run_sync` (до commit,
  сбой — ошибка элемента), удаление точек пропавших документов, `ensure_collection` и сборка
  в воркере. DoD: тесты пайплайна на фейковых эмбеддере и индексе.
- [x] **P4-07a search_knowledge: гибридный поиск.** `ChunkIndex.search` (Query API: dense +
  BM25, IDF по тенанту, RRF), порт `Reranker`, use case `KnowledgeSearch` (кандидаты под
  реранкинг, откат к RRF при сбое реранкера). DoD: тест изоляции тенантов в Qdrant, тест
  качества на мини-корпусе.
- [x] **P4-07b search_knowledge: инструмент.** Встроенный инструмент (`query`, `top_k`),
  `content` для модели, компонент `sources` (фрагменты с url), ошибки → `upstream_error`;
  сборка `KnowledgeSearch` в API и раннере эвалов. DoD: тесты инструмента, тест цикла на
  FakeLLM с компонентом `sources`.
- [x] **P4-07c Реранкер.** HTTP-адаптер `/rerank` в формате Cohere/Jina (httpx), env
  `RERANK_URL`/`RERANK_API_KEY`/`RERANK_MODEL`, без env — реранкинг выключен; ADR-0015.
- [x] **P4-08a Запрос каталога.** `EntityRepository.search` (`CatalogQuery`): фильтры по
  нормализованным полям и attributes, сортировка, `query` — полнотекстовый поиск Postgres
  (ADR-0016); `SqlCatalog` (своя сессия на вызов) и порт tools `Catalog`.
  DoD: тесты фильтров на Postgres, изоляция тенантов.
- [x] **P4-08b search_catalog / get_entity: инструменты.** Схема аргументов по
  `knowledge.catalog.filterable_attributes`, `content` для модели, `not_found`/`upstream_error`,
  сборка `SqlCatalog` в API (в эвалах без БД — не подключается). DoD: тесты инструментов,
  тест цикла на FakeLLM.
- [x] **P4-09a Коннектор http_api.** Общий маппинг сущностей/документов (`entity_mapping`,
  `table` на нём же), секреты источников — ссылки на env `SOURCE_SECRET_*` (ADR-0017),
  GET через `WebClient` (ADR-0013), путь к записям, пагинация page/offset/cursor/next_url.
  DoD: тесты на `httpx.MockTransport`, `run_sync` на Postgres.
- [x] **P4-09b Коннектор database.** PostgreSQL (asyncpg), SQL-запрос из конфига в READ ONLY
  транзакции, `statement_timeout`, лимит строк; адрес — публичный или из allowlist подсетей
  платформы (`SOURCE_DB_ALLOWED_NETWORKS`), ADR-0018. DoD: тесты на Postgres.
- [x] **P4-10a Списочные атрибуты каталога.** Тип атрибута `list` в маппинге `table`/
  `database`/`http_api` (строка с разделителем или массив источника), фильтр `any_of` по
  массиву — вхождение элемента. DoD: тесты маппинга и фильтров на Postgres.
- [x] **P4-10b Загрузка источников тенанта.** `sources` в YAML тенанта, `Source.name`,
  seed источников с копированием файлов в каталог источника, `app.cli sync`, `make pilot`;
  раннер эвалов берёт `tenant_id` из БД по slug и подключает каталог (ADR-0011 п.5), ADR-0019.
  DoD: тесты seed источников и раннера.
- [x] **P4-10c Данные пилота.** 15 MD-страниц базы знаний и каталог ~100 позиций
  (`data/pilot/demo-beauty/`), маппинг в конфиге тенанта, `knowledge.catalog.entity_types`.
  DoD: `make pilot`, `make eval` — вопросы по знаниям проходят (2 из 4; beauty_08 ход 3 и
  beauty_16 — поведение промпта, перенесено в P6-03).

## P5. Rich UI

- [x] **P5-01 show_entities.** Инструмент → product_card / product_carousel / comparison_table.
- [x] **P5-02 Рендер компонентов.** React-компоненты по сгенерированным типам, Storybook или
  демо-страница со всеми вариантами. DoD: Playwright-скриншоты.
- [x] **P5-03a Действия и быстрые ответы: бэкенд.** `suggest_replies` → SSE `suggestions`,
  кнопки карточек из `knowledge.catalog.card_actions`, `ActionInput.label` (история и модель),
  ADR-0020. DoD: тесты на FakeLLM и SSE.
- [x] **P5-03b Действия и быстрые ответы: фронт.** Нажатие Action → `input.type=action`
  с `label`, чипы `suggestions` под последним ответом, `starter_suggestions` из public config
  на пустом чате, подпись выбора в истории. DoD: unit-тесты reducer'а, e2e.
- [x] **P5-04a Формы и подтверждения: бэкенд.** `show_form`, `create_lead` с
  requires_confirmation — ожидающий вызов в DialogState, исполнение по кнопке `confirm` без
  модели, `assistant.confirm_labels`, модуль `leads`, проверка `form_submit` (422), ADR-0021.
  DoD: тесты на FakeLLM, SSE и Postgres.
- [x] **P5-04b Формы и подтверждения: фронт.** Отправка формы (`form_submit` с `label` —
  заголовок формы в истории), кнопки confirm, блокировка во время хода. DoD: unit-тесты
  reducer'а, e2e (show_form → форма → confirm → заявка через мок модели).
- [x] **P5-05 Брендинг.** Токены из public config → CSS-переменные, логотип, приветствие.
  DoD: два демо-тенанта выглядят по-разному.
- [x] **P5-06a Виджет: бэкенд.** `GET /v1/public/widget` → `WidgetEmbed {allowed_origins}`,
  проверка `Origin` по ключу (403 `forbidden`, свой чат — `CORS_ORIGINS`), формат origin
  в YAML тенанта, ADR-0022. DoD: тесты API и spec.
- [x] **P5-06b Виджет: фронт.** `widget.js` + iframe `/widget`, CSP `frame-ancestors` из
  `/v1/public/widget` в Next proxy (`'none'` при ошибке), кнопка открытия и «Свернуть»
  (postMessage), мобильная вёрстка, демо-страница; e2e-origin в `allowed_origins` демо-тенантов.
  DoD: unit-тесты CSP, e2e (открыть/свернуть, мобильный, чужой сайт — iframe заблокирован).
- [x] **P5-07 UX-полировка.** Статусы действий (`display_label` в `tool_started`), прерывание
  генерации («Остановить» → cancel), повтор при ошибке (`retryable`), восстановление диалога
  после перезагрузки, автоскролл. DoD: тесты цикла и SSE, unit-тесты reducer'а, e2e.
- [x] **P5-08a Повтор без дубля: бэкенд.** `POST /v1/conversations/{id}/messages/{message_id}/retry`
  (`message_id` — неудачный ответ) — новый ход по сохранённому вводу; ответ помечается
  заменённым (`replaced_by`; скрыт из истории клиента и контекста модели, в БД остаётся);
  409 `not_retryable`, `error {code, message, retryable}` в `HistoryMessage`, ADR-0023.
  DoD: тесты на FakeLLM, SSE и Postgres.
- [x] **P5-08b Повтор без дубля: фронт.** «Повторить» → retry-эндпоинт по id неудачного ответа
  (без ответа — повторная отправка ввода, как сейчас); `error` из истории — «Повторить» и после
  перезагрузки; 409 → перечитать историю. DoD: unit-тесты reducer'а, e2e (сбой → повтор —
  один вопрос в ленте; сбой → перезагрузка → повтор).

## P6. Качество диалога

- [x] **P6-01 Сценарии и слоты.** Scenario-слой, выбор сценария моделью, слоты из конфига.
- [x] **P6-02a Суммаризация: ядро.** `memory` в AgentConfig (порог в приблизительных токенах,
  `keep_recent_turns`, `summary_model`), политика сворачивания по границе хода, накопительная
  сводка моделью (`HistorySummarizer`), use case `summarize_conversation` с условной записью
  `summary`/`summary_message_id`, в ход — сообщения после границы и сводка в Runtime-слое.
- [x] **P6-02b Суммаризация: фоновый запуск.** После записи ответа — проверка порога
  (`summary_fold`) и asyncio-задача в процессе API со своей сессией БД, одна на диалог;
  потерянная при рестарте задача запускается следующим ходом. ADR-0024 (asyncio vs arq).
  DoD: тест — после хода с длинной историей сводка записана, следующий ход видит её; ошибка
  модели сводки не влияет на ход.
- [x] **P6-03 Итерации промптов по эвалам.** Цикл: гипотеза → правка → `make eval` → diff.
  Цель — заранее заданные пороги по рубрике. (Повторяемая задача.)
  Итерация 1 (порог — ≥ 92% проверок в среднем по 2 прогонам, без стабильных ухудшений в
  health/boundary): судья видит содержимое компонентов — названия, цены, строки сравнения
  (`JUDGE_PROMPT_VERSION` 3; цены модель текстом не пишет, ADR-0004); `show_entities` —
  варианты на выбор каруселью, cards — одна позиция; результат `create_lead` — «что дальше»
  по базе знаний; промпт demo-beauty — постепенное введение активов и тест на участке, пропуск
  шага ухода — объяснение по базе знаний и компромисс, отказ — с альтернативой, «одно
  средство» — 2–3 варианта, условия — по базе знаний, доступность — по атрибутам, не больше
  2–3 поисков, вывод после сравнения, подарок при неизвестных вкусах, контакты в support;
  эталоны — beauty_04 без подстроки «гарантир», рубрики beauty_13 оценивают ответ, а не слоты
  (P6-05). Итог: 85,1% (судья v2) → 95,5% (судья v3), диалогов ✅ 7–9 → 15 из 20; входных
  токенов агента на прогон +16% (~730 → ~850 тыс.).
  Остались (кандидаты следующей итерации): beauty_15 — «подберу готовую маску» явно;
  beauty_19 — после «точного нет» показать ближайшую связку, а не спрашивать; beauty_20 ход 2 —
  пара очищение + увлажнение; beauty_03 ход 3 — компромисс-средство с SPF; beauty_05/06 ход 2
  нестабильны (тест на участке, SPF); рост токенов — шаги поиска в beauty_19.
  Голые e-mail/URL в ответах (контакты) — `remark-gfm` в `components/chat/Markdown.tsx` отложен.
- [x] **P6-04a Язык диалога: бэкенд.** Язык диалога фиксируется при создании по `locale`
  клиента (`CreateConversationRequest.locale`) и конфигу (`assistant.language`,
  `default_language`), `Conversation.language`; при `auto` — ответ на языке сообщения клиента,
  запасной — язык диалога (Runtime-слой); тексты тенанта — `assistant.translations`, подписи
  платформы (`display_label`, кнопки confirm по умолчанию) — словарь ядра ru/en/sv;
  `GET /v1/public/config?locale=` — выбранный язык и тексты на нём. ADR-0025.
  DoD: тесты выбора языка, API и Postgres, FakeLLM (fallback, confirm, статусы на языке диалога).
- [x] **P6-04b Язык диалога: фронт.** Словарь строк интерфейса чата ru/en/sv по
  `PublicAssistant.language`, `locale` (`navigator.language`) в `GET /v1/public/config` и
  `POST /v1/conversations`, `<html lang>`, локаль `Intl` в `components/rich/format.ts`.
  DoD: unit-тесты словаря и форматирования, e2e с `locale: en-US` (интерфейс и приветствие на en).
- [x] **P6-04c Язык диалога: эвалы.** 2–3 англоязычных эталона на demo-beauty
  (`language: auto`, переводы приветствия/подсказок в YAML тенанта, `locale` в диалогах эвалов),
  итерация промпта при необходимости: ответ на языке клиента при русских данных, поиск по
  каталогу и знаниям русскими запросами. Переводы `forms` и `card_actions` — если понадобятся.
  Сделано: `locale` в диалогах эвалов, проверка `reply_language` (по алфавиту), эталоны
  beauty_21–23 (en-US и en-сообщения при ru-RU), demo-beauty — `auto` + `translations.en`,
  в промпте тенанта — поисковые запросы по-русски. Итерации: язык интерфейса — запасной язык
  ответа только до первого текстового сообщения клиента; сигнал модели — алфавит последнего
  текстового сообщения (без него после поиска по русским данным ответ нестабильно уходил
  в русский: beauty_23 3/6, beauty_21 5/8). Итог (2 прогона, судья v3): 94,7% проверок,
  русские диалоги 95,0% (база P6-03 — 95,5%), `reply_language` 6/6 и 6/6, отдельно языковые
  диалоги 3 × 20/20. Остались: beauty_17 ход 1 (помощь взамен отказа) — 1 из 2 прогонов;
  beauty_21 ход 1 — разовый пустой поиск (аргументы вызовов в отчёте не сохраняются — стоит
  добавить); кандидаты P6-03 без изменений.
- [x] **P6-04d Переводы форм и кнопок карточек.** `assistant.translations.<lang>` —
  `forms` (`title`, `label` полей, `options` select по `value`), `card_actions` (по
  `action_id`), `attribute_labels` (по ключу); `localize_config` в tenants даёт инструментам
  переведённую копию конфига, `value`/ключи не меняются — проверка формы по базовому конфигу.
  Строки платформы `show_entities` (наличие, строки сравнения, да/нет) — словарь ядра ru/en/sv
  (`CatalogTexts`). Confirm заявки (`create_lead`) — тоже на языке диалога. demo-beauty —
  переводы en. `submit_label` в `FormConfig` нет — подпись кнопки формы даёт словарь фронта.
  Тесты на FakeLLM и unit-тесты; ADR-0025 дополнен.
- [x] **P6-05 Слоты активного сценария.** Схема `update_dialog_state` — объединение слотов всех
  сценариев (P6-01), поэтому модель пишет слоты чужого сценария, а при смене сценария слоты
  прежнего остаются в состоянии и в промпте. Наблюдения: живой чат — `known_preferences`
  (`gift`) при подборе парфюма; эвалы beauty_13 ход 2 — после перехода с крема на подарок
  в состоянии `skin_type`/`concerns` и дубли `for_whom`/`recipient`/`relation`. Варианты:
  (а) схема инструмента по активному сценарию — инструменты собираются по состоянию, а не
  только по конфигу, первый ход без сценария — объединение; (б) слоты по сценариям в
  DialogState (`slots: {scenario: {...}}`) + общие слоты (budget) — меняется формат
  `state_patch` и `state_contains` эвалов; (в) при смене сценария переносить только слоты,
  объявленные в новом. Сначала ADR (`/adr`) с выбором варианта. DoD: тест на FakeLLM — после
  смены сценария в промпте нет слотов прежнего (кроме общих), чужой слот — `validation_error`
  или не попадает в состояние; beauty_13 ход 2 проходит без правки рубрики про состояние.
  Сделано (ADR-0026, вариант (б) с проверкой из (а)): слоты хранятся по сценариям
  (`{сценарий: {слот: значение}}`), формат `state_patch` прежний — плоские слоты пишутся в сценарий
  патча; в Runtime-слое — слоты только активного сценария (`DialogState.prompt_dict`), общих слотов
  нет (общее — `facts`); схема инструмента — объединение, чужой слот или слоты без сценария —
  `validation_error` по сценарию вызова (`ToolContext.active_scenario`, состояние шага); плоское
  состояние до P6-05 читается в активный сценарий. Эвалы — слоты активного сценария, у beauty_13 убраны
  оговорки про состояние. Итог (2 прогона): 124 и 125 из 131 → 95,4% (база P6-04c — 94,7%),
  beauty_13 8/8 и 8/8; входные токены агента −5% (~1,00 млн → ~0,96 млн), шагов 160/160 против 169/164.
  `validation_error` на чужой слот — 4 и 2 раза за прогон, все исправлены моделью в том же ходе.

## P7. Надёжность и наблюдаемость

- [x] **P7-01 Ретраи и fallback-модель.** По таблице architecture.md §12. DoD: тесты с FakeLLM-ошибками.
  Сделано (ADR-0027): `agent.FallbackLLM` — повтор шага основной модели (2 попытки, пауза 1 с)
  и переход на `model.fallback` (1 попытка, до конца хода, без `provider_items`) при `retryable`
  ошибке, пока клиенту ничего не отдано; ошибка после первого чанка и неповторяемая — `error`
  как раньше; HTTP-ретраи — в SDK. Пустой ответ хода (ни текста, ни компонентов, ни вызовов) —
  один повтор, затем fallback-фраза (`FinishReason.EMPTY_RESPONSE`). e2e-мок «сбой» — 2 провала.
  Не сделано: повтор после частичного ответа, fallback суммаризатора, `turn_timeout_s`.
- [x] **P7-02 AgentEvent-лог и trace_id.** Запись всех событий хода, эндпоинт для админки.
  Сделано (ADR-0028): таблица `agent_events` в `observability`; журнал хода копит
  `chat.TurnStream` (`TurnJournal`) и пишет вместе с ответом одной транзакцией (и у прерванного,
  и у неудачного хода): `turn_started` (ввод, `retry_of`), `text` (блоком), `tool_started`,
  `tool_finished` (аргументы и ошибка, результат — в ToolCall), `state_updated`, `component`,
  `suggestions`, `turn_completed`, `turn_finished` (статус, `message_id`, ошибка); trace_id —
  `X-Trace-Id` запроса хода. `GET /v1/admin/tenants/{id}/events?conversation_id|turn_id|trace_id`
  по `ADMIN_API_TOKEN` (без токена — 404). Не сделано: ретраи/fallback LLM в журнале (logs,
  P7-03), ретенция журнала, запись событий до `save()` (падение процесса посреди хода).
- [ ] **P7-03 Langfuse.** Трейсы LLM, версии промптов, стоимость.
- [ ] **P7-04 Rate limiting и защита.** Лимиты по widget key / visitor, размер ввода,
  базовая защита от prompt injection в данных источников (данные ≠ инструкции).
- [ ] **P7-05 Нагрузочный тест.** k6/locust: N параллельных диалогов, латентность до первого токена.
- [ ] **P7-06 Межпроцессные ходы.** Реестр ходов в Redis: отмена хода из любого процесса API.
  По метрикам пилота (часто ли перезагружают страницу во время ответа) — продолжение стрима
  после перезагрузки: буфер событий хода, переподключение по `seq`/`Last-Event-ID`.

## P8. Мультитенантность и админка

- [ ] **P8-01 Админ-авторизация.** Пользователи админки, роли, привязка к тенантам.
- [ ] **P8-02 Админка: конфиг.** Редактор AgentConfig (YAML + валидация), draft → active, откат.
- [ ] **P8-03 Админка: источники.** Добавление, запуск синхронизации, статусы, ошибки.
- [ ] **P8-04 Админка: диалоги.** Список, просмотр хода по шагам (AgentEvent), оценка ответа.
- [ ] **P8-05 Второй тенант.** Подключить другой бизнес только конфигурацией. DoD: ноль правок
  в ядре; всё, что пришлось менять в коде, — зафиксировать как долг/ADR.

## P9. Пилот

- [ ] **P9-01 Деплой.** Docker-образы, окружения, миграции при деплое, секреты.
- [ ] **P9-02 Пилот и разбор.** Реальные диалоги → новые эталоны в evals → итерации P6-03.
