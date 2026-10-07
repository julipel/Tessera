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
