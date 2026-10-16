# ADR-0029: Трейсинг ходов в Langfuse — порт Tracer, generation на каждую попытку модели

- Статус: принято
- Дата: 2026-10-07

## Контекст
P7-03, architecture.md §13: трейсы LLM-вызовов, версия промпта, токены, стоимость.

- Langfuse принимает трейсы только через OpenTelemetry (старый ingestion API на Cloud
  отключается 16.11.2026); актуальный Python SDK v4 построен на OTel.
- Ход — async-генераторы в отдельной задаче (`TurnStream`), ретраи и резервная модель —
  внутри `FallbackLLM` (ADR-0027); «текущий» span из контекста OTel через такие границы
  ненадёжен.
- Без глобальных синглтонов (CLAUDE.md); SDK по умолчанию регистрирует глобальный
  TracerProvider.
- Журнал хода (ADR-0028) уже связывает ход с `X-Trace-Id`.

## Решение
1. **Порт `Tracer`** в `observability.kernel`: `start_turn(TurnTraceInfo) → TurnTrace`
   (generation, tool_started/finished, finish), `shutdown`. Реализации — `LangfuseTracer`
   (`observability.infrastructure`) и `NoopTracer` (нет ключей, тесты, эвалы). Сбой SDK
   при старте трейса логируется, ход идёт без трейса.
2. **Трейс = ход.** id трейса — `X-Trace-Id` запроса хода, если это 32 hex (так его генерирует
   `TraceIdMiddleware`), иначе `Langfuse.create_trace_id(seed=…)`: один id находит и журнал
   хода, и трейс. `session_id` — диалог, тег и metadata — тенант, `turn_id`; корень — observation
   `agent` с вводом и итогом хода (ответ, finish, usage) или ошибкой («прерван» при отмене).
3. **`TracedLLM`** (agent) оборачивает клиент каждой модели внутри `FallbackLLM`: каждая
   попытка — отдельная generation (модель, системный промпт и сообщения, параметры, ответ,
   usage, `completion_start_time` по первому чанку, `level=ERROR` при ошибке). Ретраи
   и переход на резервную модель видны в трейсе.
4. **Версия промпта** — `version` каждого observation: `platform_prompt:<PLATFORM_PROMPT_VERSION>;
   agent_config:<agent_config_id>`. Prompt Management Langfuse не используется: промпты
   живут в коде и AgentConfig.
5. **Инструменты** — observation `tool` от `ToolStarted` до `ToolFinished` (аргументы,
   результат, ошибка, длительность) — по событиям хода (`traced_turn`).
6. **Observation создаются явно** (`start_observation` от корня), атрибуты трейса —
   `propagate_attributes` на время создания каждого: в SDK v4 они нужны на каждом observation.
7. **Свой TracerProvider** у клиента Langfuse — глобальный провайдер OTel не регистрируется.
   Отправка — фоновым экспортёром SDK; `shutdown` (досылка) — в lifespan приложения.
   Отправка сжимается (gzip), таймаут — `LANGFUSE_TIMEOUT_S` (30 с): generation несёт весь
   промпт шага, и на медленном канале (прокси, ~30 КБ/с) пачка без сжатия не укладывалась
   в 5 с по умолчанию — SDK отбрасывал её после неудачной отправки.
8. **Стоимость** считает Langfuse по `model` и usage (встроенные цены OpenAI, Anthropic);
   для моделей `openai_compatible` (прокси) — custom models в Langfuse. Своей таблицы цен нет.

## Альтернативы
- **Свой OTLP/JSON-экспорт через httpx** — без новой зависимости, но батчинг, ретраи и фоновая
  отправка своими силами.
- **Декоратор `@observe` / «текущий» span** — проще в коде, но контекст OTel через
  async-генераторы и задачу хода переносится ненадёжно, а ошибка detach ломает ход.
- **Generation на шаг, а не на попытку** — меньше observation, но ретраи и fallback не видны.
- **Трейсинг в chat (TurnStream)** — chat не видит запросов к модели; агент — видит.

## Последствия
- Новая зависимость `langfuse` (и `opentelemetry-sdk`, OTLP-экспортёр).
- В Langfuse уходит полный текст диалога и результатов инструментов (персональные данные):
  маскирование (`mask` SDK) — отдельной задачей до пилота с реальными клиентами.
- Сводки истории (фоновый вызов LLM) и эвалы пока не трейсятся.
- Клиент SDK кэшируется по `public_key` внутри SDK — один на процесс.

## Дополнение (P9-01ab, 2026-10-16): кэш промпта в стоимости, metadata, остановка
- **Корзины токенов** (`REVIEW.md`: O-7). Langfuse считает `usage_details` непересекающимися
  корзинами: сумма корзин — весь вход, каждая умножается на свою цену модели. Раньше кэш
  промпта входил в `input` и оценивался по полной цене (чтение из кэша у Anthropic — 0,1x).
  Теперь `Usage` несёт `cache_read_tokens` и `cache_write_tokens` — доли `input_tokens`, сам
  `input_tokens` по-прежнему весь вход (бюджет хода, журнал, `TurnCompleted` не меняются).
  Трейсер раскладывает их по провайдеру (`GenerationInfo.provider`): Anthropic — `input` (вне
  кэша), `cache_read_input_tokens`, `cache_creation_input_tokens`, `output`; OpenAI и
  `openai_compatible` — `input`, `input_cached_tokens`, `output` (запись в кэш OpenAI отдельно
  не тарифицирует). Имена — как в ценах моделей Langfuse; для custom models прокси задавать
  цены с теми же ключами.
- **Оборванные попытки.** `LLMError.usage` — токены, известные до ошибки (у Anthropic вход
  известен с `message_start`, у OpenAI usage приходит в конце стрима или в `response.failed`);
  `generation.fail` отправляет их. При отмене хода usage нет — ограничение.
- **Metadata трейса** (O-11): версии слоёв промпта — одним ключом `prompt_versions` (JSON:
  SDK приводит значения metadata к строкам), чтобы не затирать `tenant_id`/`turn_id`.
  `version` observation не меняется.
- **Остановка** (O-12): uvicorn ждёт ходы не дольше 15 с (`--timeout-graceful-shutdown`),
  затем отменяет их и сразу зовёт lifespan. Lifespan до 3 с ждёт, пока прерванные ходы
  снимутся с реестра (`TurnRegistry.wait_idle`: журнал и трейс дописаны), и только потом
  закрывает трейсер, Redis и БД. `stop_grace_period: 60s` у `api` в `compose.prod.yml`
  (по умолчанию Docker ждёт 10 с и шлёт SIGKILL). Ограничение: `shutdown` SDK не имеет
  общего таймаута — `LANGFUSE_TIMEOUT_S` действует на каждую пачку (OTel `force_flush`
  выгружает всю очередь), поэтому при недоступном Langfuse остановку обрывает SIGKILL на 60 с,
  а хвост трейсов теряется; при доступном отправка укладывается в секунды.
