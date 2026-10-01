# Контракты

Источник истины — JSON Schema в `packages/contracts/schemas/`. Этот документ описывает смысл
и примеры. `make contracts` генерирует:
- `apps/api/src/app/contracts/generated/` — Pydantic v2 (datamodel-code-generator)
- `apps/web/src/contracts/generated/` — TypeScript (json-schema-to-typescript)

Версия протокола: `protocol_version: "1"`. Обратно несовместимые изменения — только
с повышением версии и ADR.

## 1. HTTP API (публичный, для чата/виджета)

Авторизация: заголовок `X-Widget-Key`. Посетитель: `visitor_id` (генерирует клиент, хранит у себя).

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/public/config` | брендинг, приветствие, стартовые подсказки |
| POST | `/v1/conversations` | создать диалог → `{conversation_id}` |
| GET | `/v1/conversations/{id}/messages` | история (для восстановления) |
| POST | `/v1/conversations/{id}/messages` | отправить ввод, ответ — SSE-стрим |
| POST | `/v1/conversations/{id}/turns/{turn_id}/cancel` | прервать генерацию |

Тело `POST .../messages`:
```json
{
  "client_message_id": "uuid",           // идемпотентность
  "input": { "type": "text", "text": "Нужен подарок маме, до 5000" }
}
```
или нажатие кнопки / отправка формы:
```json
{
  "client_message_id": "uuid",
  "input": { "type": "action", "action_id": "select_product", "payload": { "entity_id": "e_123" } }
}
```
```json
{
  "client_message_id": "uuid",
  "input": { "type": "form_submit", "form_id": "f_1", "values": { "phone": "+46..." } }
}
```

## 2. SSE-протокол

Каждое событие: `event: <type>` и `data: <Envelope JSON>`.

```json
{
  "protocol_version": "1",
  "seq": 7,
  "type": "text_delta",
  "conversation_id": "c_...",
  "turn_id": "t_...",
  "message_id": "m_...",
  "ts": "2026-09-28T12:00:00Z",
  "data": { }
}
```

| type | data | Назначение |
|---|---|---|
| `turn_started` | `{}` | начало хода ассистента |
| `text_delta` | `{ "block_id": "b1", "delta": "..." }` | кусок текста (markdown) |
| `text_done` | `{ "block_id": "b1" }` | текстовый блок завершён |
| `status` | `{ "kind": "searching" \| "thinking" \| "calling_api", "label": "Ищу в каталоге…" }` | индикатор действия |
| `tool_started` | `{ "tool_call_id", "name", "display_label" }` | инструмент начал работу |
| `tool_finished` | `{ "tool_call_id", "ok": true, "duration_ms" }` | инструмент завершился (без сырых данных) |
| `component` | `{ "block_id": "b2", "component": Component }` | UI-компонент (см. §3) |
| `suggestions` | `{ "items": [{ "label", "input": UserInput }] }` | быстрые ответы под сообщением |
| `error` | `{ "code", "message", "retryable": bool }` | ошибка хода |
| `done` | `{ "status": "completed" \| "interrupted" \| "failed", "usage": {...} }` | конец хода |

Правила: `seq` монотонно растёт в пределах хода; блоки сообщения упорядочены по первому
появлению `block_id`; клиент игнорирует неизвестные `type`. Сырые результаты инструментов
в клиент не отправляются.

## 3. UI-компоненты

Discriminated union по полю `type`. Все URL и цены — из данных, не от модели.

```json
{ "type": "product_card", "entity_id": "e_1", "title": "...", "subtitle": "...",
  "image_url": "...", "price": { "amount": 4990, "currency": "SEK" },
  "badges": ["В наличии"], "url": "...",
  "actions": [ Action ] }
```
```json
{ "type": "product_carousel", "title": "Подходящие варианты", "items": [ ProductCard ] }
```
```json
{ "type": "info_card", "title": "...", "body_markdown": "...", "image_url": null, "url": null }
```
```json
{ "type": "image", "url": "...", "alt": "...", "caption": null }
```
```json
{ "type": "link_list", "items": [{ "title": "...", "url": "...", "description": "..." }] }
```
```json
{ "type": "sources", "items": [{ "title": "...", "url": "...", "snippet": "..." }] }
```
```json
{ "type": "comparison_table", "columns": ["Модель A", "Модель B"],
  "rows": [{ "label": "Цена", "values": ["4990 SEK", "5490 SEK"] }] }
```
```json
{ "type": "form", "form_id": "f_1", "title": "Оставьте контакт",
  "fields": [{ "name": "phone", "label": "Телефон", "kind": "phone", "required": true }],
  "submit_label": "Отправить" }
```
`kind` поля формы: `text` \| `phone` \| `email` \| `textarea` \| `select` \| `date` (`select`
требует `options`). Контакт одним полем (телефон или Telegram) — `kind: text`, не `phone`.
```json
{ "type": "confirm", "confirm_id": "cf_1", "text": "Создать заявку на ...?",
  "confirm_action": Action, "cancel_action": Action }
```

`Action`:
```json
{ "action_id": "select_product", "label": "Выбрать", "style": "primary" | "secondary" | "link",
  "payload": { }, "url": null }
```
Если `url` задан — это ссылка; иначе нажатие отправляет `input.type = "action"`.

## 4. Интерфейс инструментов

```python
class ToolDefinition(BaseModel):
    name: str                      # snake_case, уникален в конфиге тенанта
    description: str               # для модели: когда и зачем вызывать
    parameters: dict               # JSON Schema аргументов
    timeout_s: float = 10
    side_effect: bool = False      # меняет что-то во внешнем мире
    requires_confirmation: bool = False
    display_label: str | None      # «Ищу в каталоге…»

class ToolResult(BaseModel):
    content: str | dict            # компактно, для модели
    components: list[Component] = []
    state_patch: dict | None = None
    error: ToolError | None = None

class ToolError(BaseModel):
    code: Literal["validation_error", "not_found", "upstream_error", "timeout", "forbidden"]
    message: str                   # понятно модели
    retryable: bool
```

Встроенные инструменты:

| Имя | Назначение |
|---|---|
| `search_knowledge(query, filters?, top_k?)` | гибридный поиск по документам, возвращает фрагменты + sources |
| `search_catalog(query?, filters?, sort?, limit?)` | структурный поиск Entity |
| `get_entity(entity_id)` | детали сущности |
| `show_entities(entity_ids, layout: "cards" \| "carousel" \| "comparison")` | UI-компоненты по id |
| `update_dialog_state(slots?, facts?)` | запись собранной информации |
| `show_form(form_key)` | форма из конфига тенанта |
| `create_lead(fields)` | заявка (side_effect, requires_confirmation) |
| `handoff_to_human(reason)` | передача оператору (позже) |

Декларативный HTTP-инструмент тенанта (в AgentConfig):
```yaml
- name: check_availability
  kind: http
  description: "Проверить наличие товара в магазине по городу"
  parameters:
    type: object
    properties:
      entity_id: { type: string }
      city: { type: string }
    required: [entity_id, city]
  request:
    method: GET
    url: "https://api.example.com/stock/{entity_id}?city={city}"
    auth: { type: bearer, secret_ref: "EXAMPLE_API_TOKEN" }
  response:
    content_jmespath: "{available: available, stores: stores[].name}"
  timeout_s: 5
```

## 5. AgentConfig

```yaml
assistant:
  name: "Ассистент Example"
  language: auto              # или ru / en / sv
  greeting: "Привет! Помогу подобрать..."
  starter_suggestions: ["Подобрать подарок", "Условия доставки"]
  fallback_message: "Извините, сейчас не получается ответить. Попробуйте ещё раз."
model:
  primary: { provider: openai, name: "<model>", temperature: 0.3 }
  fallback: { provider: anthropic, name: "<model>" }
limits: { max_steps: 6, max_tool_calls_per_step: 4, turn_timeout_s: 60, max_tool_retries: 2 }
prompt:
  tenant: |
    Ты — консультант компании Example...
  scenarios:
    - key: product_selection
      description: "Подбор товара под задачу пользователя"
      instructions: |
        Выясни назначение, бюджет...
      slots:
        budget: { type: number, description: "Бюджет" }
        recipient: { type: string }
tools:
  builtin: [search_knowledge, search_catalog, get_entity, show_entities, update_dialog_state]
  custom: [ ...HTTP-инструменты... ]
forms:
  contact: { title: "...", fields: [...] }
knowledge:
  search_knowledge: { top_k: 6, rerank: true }
  catalog: { filterable_attributes: [color, size, material] }
branding:
  tokens: { primary: "#1F4FFF", radius: "12px", font: "Inter" }
  logo_url: "..."
```

## 6. Коды ошибок (`error.code`)

`llm_unavailable` (retryable), `turn_timeout` (retryable), `step_limit`, `invalid_input`,
`conversation_not_found`, `rate_limited` (retryable), `internal`.
