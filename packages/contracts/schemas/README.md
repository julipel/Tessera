# JSON Schema контрактов

Источник истины для связи бэкенда и фронтенда (см. docs/contracts.md). Заполнено в P0-03,
с поправками под пилот `demo-beauty` (docs/pilot.md): явный enum `kind` для полей формы
(нужны `text`/`textarea`, не только `phone` — форма `consultation` собирает имя, контакт
телефоном-или-Telegram одним текстовым полем, время, комментарий) и декларативный
HTTP-инструмент `HttpToolDefinition` для `check_stock`.

- `envelope.schema.json` — SSE-конверт (без типизации `data`, см. events.schema.json)
- `events.schema.json` — все события по type: `turn_started`, `text_delta`, `text_done`,
  `status`, `tool_started`, `tool_finished`, `component`, `suggestions`, `error`, `done`
- `components.schema.json` — UI-компоненты (`product_card`, `product_carousel`, `info_card`,
  `image`, `link_list`, `sources`, `comparison_table`, `form`, `confirm`), `Action`, `Price`,
  `FormField` (kind: text/phone/email/textarea/select/date; `select` требует `options`)
- `user_input.schema.json` — `text` / `action` / `form_submit`
- `tools.schema.json` — `ToolDefinition`, `ToolResult`, `ToolError`, `HttpToolDefinition`
  (декларативный HTTP-инструмент тенанта)
- `agent_config.schema.json` — конфигурация тенанта целиком (assistant/model/limits/prompt/
  tools/forms/knowledge/branding)
- `conversations.schema.json` — HTTP API диалогов: `CreateConversationRequest/Response`,
  `SendMessageRequest`, `MessageHistory`, `HistoryMessage` (`input` у user, `blocks` у
  assistant), `MessageBlock` (`TextBlock` / `ComponentBlock`)
- `http_error.schema.json` — тело любого HTTP-ответа с ошибкой: `{"error": {code, message,
  retryable}}`; коды SSE-ошибок + `unauthorized`/`not_found`/`duplicate_message`
  (docs/contracts.md §1, §6)
- `public_config.schema.json` — ответ `GET /v1/public/config`: публичная часть активного
  AgentConfig (assistant без fallback_message, branding), без промпта/инструментов/моделей

Файлы ссылаются друг на друга относительными `$ref` (напр. `events.schema.json` →
`components.schema.json`, `agent_config.schema.json` → `tools.schema.json#/$defs/HttpToolDefinition`
и `components.schema.json#/$defs/FormField`) — при генерации держать все файлы в этой папке рядом.

Проверено (Draft 2020-12, пакет `jsonschema`): все файлы валидны как схемы, кросс-файловые
`$ref` разрешаются, примеры из docs/contracts.md и `evals/dialogs/beauty_18_lead_consultation.yaml`
проходят валидацию, негативные кейсы (неизвестный `type`, `select` без `options`, лишнее поле
в AgentConfig) корректно отклоняются.

Намеренно не фиксировано в схеме (специфика пилота, а не контракт ядра): атрибуты Entity
(`skin_types`, `concerns`, `families` и т.п. из docs/pilot.md §3) — они живут в данных каталога
и в `filterable_attributes`/`slots` конкретного `AgentConfig`, а не в JSON Schema контрактов.

Генерация: `make contracts` (`scripts/contracts.sh`) → Pydantic v2 в
`apps/api/src/app/contracts/generated/` (datamodel-code-generator) и TypeScript в
`apps/web/src/contracts/generated/index.ts` (json-schema-to-typescript,
`packages/contracts/scripts/generate-ts.mjs`). Импортировать из точек входа `app.contracts` и
`@/contracts`. `make check` (цель `contracts-check`) падает, если generated не совпадает
со схемами. Сгенерированный код не редактировать.

Ограничения генерации: `if/then` (у `select` должны быть `options`) в Pydantic не переносится —
проверять в коде, который строит форму. Вложенным объектам задаётся `title` (напр. `TextDeltaData`) —
иначе генератор называет их `Data`, `Data1`… и имена сдвигаются при добавлении новых.
