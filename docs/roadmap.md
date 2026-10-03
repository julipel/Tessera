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
- [ ] **P4-02b arq-воркер ingestion.** `app/worker.py`, постановка синхронизации в очередь
  (порт SyncQueue), защита от параллельного запуска одного источника. DoD: тест задачи воркера.
- [ ] **P4-03 Коннектор file.** PDF/DOCX/MD/TXT → Document. Структурный чанкинг.
- [ ] **P4-04 Коннектор table.** CSV/XLSX → Entity по маппингу колонок из конфига.
- [ ] **P4-05 Коннектор website.** Sitemap + краулер с лимитами, извлечение основного контента.
- [ ] **P4-06 Индексация в Qdrant.** Эмбеддинги (порт + реализация), dense+sparse, payload с tenant_id.
- [ ] **P4-07 search_knowledge.** Гибридный поиск + RRF + опциональный реранкинг, компонент `sources`.
  DoD: тест изоляции тенантов в Qdrant, тест качества на мини-корпусе.
- [ ] **P4-08 search_catalog / get_entity.** Фильтры по нормализованным полям и attributes,
  сортировка, опциональный семантический запрос. DoD: тесты фильтров.
- [ ] **P4-09 Коннектор http_api и database.** Декларативный маппинг, read-only.
- [ ] **P4-10 Загрузка данных пилота.** Все источники пилота проиндексированы. DoD: `make eval` —
  вопросы по знаниям проходят.

## P5. Rich UI

- [ ] **P5-01 show_entities.** Инструмент → product_card / product_carousel / comparison_table.
- [ ] **P5-02 Рендер компонентов.** React-компоненты по сгенерированным типам, Storybook или
  демо-страница со всеми вариантами. DoD: Playwright-скриншоты.
- [ ] **P5-03 Действия и быстрые ответы.** Action → `input.type=action`, suggestions,
  отображение выбора в истории.
- [ ] **P5-04 Формы и подтверждения.** form, confirm, `create_lead` с requires_confirmation.
- [ ] **P5-05 Брендинг.** Токены из public config → CSS-переменные, логотип, приветствие.
  DoD: два демо-тенанта выглядят по-разному.
- [ ] **P5-06 Виджет.** `widget.js` + iframe, allowed_origins, кнопка открытия, мобильная вёрстка.
- [ ] **P5-07 UX-полировка.** Статусы действий, прерывание генерации, повтор при ошибке,
  восстановление диалога после перезагрузки, автоскролл.

## P6. Качество диалога

- [ ] **P6-01 Сценарии и слоты.** Scenario-слой, выбор сценария моделью, слоты из конфига.
- [ ] **P6-02 Суммаризация.** Фоновая сводка длинной истории, порог по токенам.
- [ ] **P6-03 Итерации промптов по эвалам.** Цикл: гипотеза → правка → `make eval` → diff.
  Цель — заранее заданные пороги по рубрике. (Повторяемая задача.)
- [ ] **P6-04 Мультиязычность.** Язык ответа по пользователю / конфигу.

## P7. Надёжность и наблюдаемость

- [ ] **P7-01 Ретраи и fallback-модель.** По таблице architecture.md §12. DoD: тесты с FakeLLM-ошибками.
- [ ] **P7-02 AgentEvent-лог и trace_id.** Запись всех событий хода, эндпоинт для админки.
- [ ] **P7-03 Langfuse.** Трейсы LLM, версии промптов, стоимость.
- [ ] **P7-04 Rate limiting и защита.** Лимиты по widget key / visitor, размер ввода,
  базовая защита от prompt injection в данных источников (данные ≠ инструкции).
- [ ] **P7-05 Нагрузочный тест.** k6/locust: N параллельных диалогов, латентность до первого токена.

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
