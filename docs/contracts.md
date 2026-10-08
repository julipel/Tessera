# Контракты

Источник истины — JSON Schema в `packages/contracts/schemas/`. Этот документ описывает смысл
и примеры. `make contracts` генерирует:
- `apps/api/src/app/contracts/generated/` — Pydantic v2 (datamodel-code-generator)
- `apps/web/src/contracts/generated/` — TypeScript (json-schema-to-typescript)

Версия протокола: `protocol_version: "1"`. Обратно несовместимые изменения — только
с повышением версии и ADR.

## 1. HTTP API (публичный, для чата/виджета)

Авторизация: заголовок `X-Widget-Key`. Посетитель: `visitor_id` (генерирует клиент, хранит у себя).
Запрос с `Origin`, которого нет в `allowed_origins` ключа и среди origin своего веб-чата
(`CORS_ORIGINS`), — 403 `forbidden`; без `Origin` (не браузер) — пропускается (ADR-0022).

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v1/public/config?locale=` | брендинг, приветствие, стартовые подсказки на языке диалога (ADR-0025) |
| GET | `/v1/public/widget` | `WidgetEmbed {allowed_origins}` — для CSP `frame-ancestors` iframe (ADR-0022) |
| POST | `/v1/conversations` | создать диалог → `{conversation_id}` |
| GET | `/v1/conversations/{id}/messages` | история (для восстановления) |
| POST | `/v1/conversations/{id}/messages` | отправить ввод, ответ — SSE-стрим |
| POST | `/v1/conversations/{id}/messages/{message_id}/retry` | повторить неудачный ответ, ответ — SSE-стрим |
| POST | `/v1/conversations/{id}/turns/{turn_id}/cancel` | прервать генерацию |

`POST /v1/conversations`: тело `{"visitor_id": "...", "locale": "en-US"}` → 201
`{"conversation_id": "uuid"}`. Диалог запоминает активную версию AgentConfig; нет активной →
404 `not_found`. `locale` (необязательный, BCP 47, `navigator.language`) выбирает язык диалога
один раз (ADR-0025): фиксированный `assistant.language` или, при `auto`, основной язык `locale`,
если он поддерживается (ru / en / sv), иначе `assistant.default_language`. На языке диалога —
тексты тенанта, подписи платформы (`display_label`, кнопки confirm по умолчанию) и запасной язык
ответа; `GET /v1/public/config?locale=` выбирает язык тем же правилом и отдаёт его
в `assistant.language` (без `auto`). Веб-чат передаёт `navigator.language` в оба запроса,
строки интерфейса (`apps/web/src/lib/i18n.ts`) и локаль цен — по `assistant.language`.

`GET .../messages` → `MessageHistory` (`conversations.schema.json`): сообщения в порядке создания,
у `user` — исходный `input`, у `assistant` — `blocks` (`text` / `component` с `block_id`, порядок
как в стриме), `status`: `completed` | `interrupted` | `failed`. У неудачного ответа — `error`
`{code, message, retryable}` (как SSE-событие `error`). Ответы, заменённые повтором, в историю
не попадают. Чужой/несуществующий диалог — 404 `conversation_not_found`.

Тело `POST .../messages` (`SendMessageRequest`):
```json
{
  "client_message_id": "uuid",           // идемпотентность: повтор в том же диалоге не создаёт
                                         // второе сообщение и не запускает ход → 409
                                         // `duplicate_message`, ответ — из истории
  "input": { "type": "text", "text": "Нужен подарок маме, до 5000" }
}
```
или нажатие кнопки / отправка формы:
```json
{
  "client_message_id": "uuid",
  "input": { "type": "action", "action_id": "select_product", "label": "Выбрать — Linen Morning",
             "payload": { "entity_id": "e_123" } }
}
```
`label` (необязательно) — что увидел пользователь: подпись кнопки, при необходимости с названием
позиции. Показывается в истории вместо `action_id` и передаётся модели вместе с `action_id`
и `payload`. Быстрый ответ (`suggestions`) отправляется обычным `input.type=text`.
```json
{
  "client_message_id": "uuid",
  "input": { "type": "form_submit", "form_id": "f_1", "label": "Оставьте контакт",
             "values": { "phone": "+46..." } }
}
```
`label` (необязательно) — заголовок формы: показывается в истории вместо значений. Модель
получает `form_id` и `values`; пустые необязательные поля клиент не отправляет.

`form_submit` проверяется по форме `forms[form_id]` конфига до записи сообщения: неизвестная
форма, лишнее поле, пустое обязательное, не строка или значение `select` не из `options` —
422 `invalid_input`, ввод не сохраняется.

`POST .../messages/{message_id}/retry` (без тела; ADR-0023): `message_id` — неудачный ответ
ассистента. Новый ход по тому же вводу, без нового сообщения пользователя. Стрим такой же, как
у `POST .../messages`, с новым `message_id`. Неудачный ответ помечается заменённым: его нет в
истории и в контексте модели, в БД он остаётся. Повторить можно только последний ответ
диалога со `status=failed` и `error.retryable=true`, иначе — 409 `not_retryable`. Ответа нет
в диалоге (или он уже заменён) — 404 `not_found`.

`POST .../turns/{turn_id}/cancel` (`turn_id` — из событий хода): 204 — отмена принята, стрим
хода закончится `done{interrupted}`, частичный ответ сохранится в истории; 404 `not_found` —
хода нет, он уже завершён или принадлежит другому диалогу/тенанту.

Ошибки HTTP (4xx/5xx) — всегда в одном формате (`http_error.schema.json`), коды из §6:
```json
{ "error": { "code": "unauthorized", "message": "неизвестный ключ виджета", "retryable": false } }
```
Статусы: 401 `unauthorized` (нет/неверный `X-Widget-Key`), 403 `forbidden` (`Origin` не
разрешён для ключа), 404 `not_found` /
`conversation_not_found`, 409 `duplicate_message` / `not_retryable`, 405/422 `invalid_input`, 429 `rate_limited`, 500 `internal`
(подробности — только в логе с `trace_id`; `X-Trace-Id` есть в любом ответе).

Лимиты (P7-04a, ADR-0030): `POST /v1/conversations` — 30 в час с IP; ходы (`POST …/messages`,
`…/retry`) — 20 в минуту с IP и 300 в минуту на тенанта (значения — настройки платформы).
Превышение — 429 `rate_limited` (retryable) с заголовком `Retry-After` (секунды до нового окна).
Ввод хода: `text` — до 4000 символов (`user_input.schema.json`), тело запроса — до 32 КБ;
больше — 422 `invalid_input`, ввод не записывается.

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
| `tool_started` | `{ "tool_call_id", "name", "display_label" }` | инструмент начал работу; `display_label` — подпись из `ToolDefinition` или `null` |
| `tool_finished` | `{ "tool_call_id", "ok": true, "duration_ms" }` | инструмент завершился (без сырых данных) |
| `component` | `{ "block_id": "b2", "component": Component }` | UI-компонент (см. §3) |
| `suggestions` | `{ "items": [{ "label", "input": UserInput }] }` | быстрые ответы под сообщением |
| `error` | `{ "code", "message", "retryable": bool }` | ошибка хода |
| `done` | `{ "status": "completed" \| "interrupted" \| "failed", "usage": {...} }` | конец хода |

Ошибки ввода и доступа (401/404/409/422) приходят обычным HTTP-ответом до начала стрима.
Ход без ошибок: `turn_started` → `text_delta`… → `text_done` → `done`; сбой агента —
`error` → `done{failed}`. `seq` начинается с 1. Ответ ассистента записан в историю до `done`;
при обрыве соединения клиентом частичный ответ сохраняется со статусом `interrupted`.

`suggestions` приходит из инструмента `suggest_replies` (ADR-0020) в любой момент хода; если
событий несколько, действует последнее. Подсказки показываются под последним ответом, пока
пользователь не отправил новый ввод; в историю не сохраняются.

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
`price` необязателен: у позиции без цены или валюты — `null`.
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
Кнопки `confirm` создаёт Tool Registry (ADR-0021): `action_id` — `confirm` / `cancel`,
`payload: {confirm_id}`, подписи — `assistant.confirm_labels`. Форма — из `forms` конфига
(`show_form`), `form_id` = ключ формы.
Кнопки `product_card` из `show_entities` — из `knowledge.catalog.card_actions` тенанта
(`payload: {entity_id}`); у `comparison_table` кнопок нет.

## 4. Интерфейс инструментов

```python
class ToolDefinition(BaseModel):
    name: str                      # snake_case, уникален в конфиге тенанта
    description: str               # для модели: когда и зачем вызывать
    parameters: dict               # JSON Schema аргументов
    timeout_s: float = 10
    side_effect: bool = False      # меняет что-то во внешнем мире
    requires_confirmation: bool = False  # исполняется после confirm пользователя (ADR-0021)
    display_label: str | None      # «Ищу в каталоге…»

class ToolResult(BaseModel):
    content: str | dict            # компактно, для модели
    components: list[Component] = []
    state_patch: dict | None = None
    suggestions: list[str] = []    # быстрые ответы → SSE `suggestions`
    error: ToolError | None = None

class ToolError(BaseModel):
    code: Literal["validation_error", "not_found", "upstream_error", "timeout", "forbidden"]
    message: str                   # понятно модели
    retryable: bool
```

`state_patch` — частичное обновление DialogState (architecture.md §7):
```json
{ "slots": { "budget": 3000, "skin_type": null }, "facts": ["аллергия на отдушки"],
  "shown_entities": ["e_1"], "active_scenario": "skincare",
  "pending_confirmation": { "confirm_id": "cf_1", "tool": "create_lead", "arguments": {} } }
```
Все ключи необязательны. `slots` сливаются со слотами сценария патча (`active_scenario` патча,
иначе текущего; без сценария не пишутся; хранятся по сценариям, ADR-0026), `null` удаляет слот;
`facts` и `shown_entities` дописываются без повторов; `active_scenario` и
`pending_confirmation` заменяются (`null` снимает ожидание; ставит его только Registry). Патчи
неуспешных вызовов не применяются. Аргументы `update_dialog_state` — по слотам сценариев
AgentConfig (лишний слот или неверный тип — `validation_error`); слот с одним именем и разными
определениями в разных сценариях принимает значение по любому из них. Слоты не из сценария
вызова (`scenario`, без него — активного) или без сценария — `validation_error`. `scenario` — ключ
сценария из конфига (или `null`), в патче становится `active_scenario`.

Встроенные инструменты:

| Имя | Назначение |
|---|---|
| `search_knowledge(query, filters?, top_k?)` | гибридный поиск по документам, возвращает фрагменты + sources |
| `search_catalog(query?, filters?, sort?, limit?)` | структурный поиск Entity: фильтры по полям и `filterable_attributes`, `query` — полнотекстовый (ADR-0016) |
| `get_entity(entity_id)` | детали сущности; неизвестный id — `not_found` |
| `show_entities(entity_ids, layout: "cards" \| "carousel" \| "comparison", title?)` | UI-компоненты по id из БД (ADR-0004) |
| `update_dialog_state(slots?, facts?, scenario?)` | запись собранной информации и выбор сценария |
| `suggest_replies(options)` | 1–4 быстрых ответа (≤ 40 символов) от лица пользователя → SSE `suggestions` (ADR-0020) |
| `show_form(form_key)` | компонент `form` из `forms` конфига тенанта |
| `create_lead(form_key, fields)` | заявка по полям формы (side_effect, requires_confirmation, ADR-0021); поля — строки, проверяются по форме |
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
  language: auto              # или ru / en / sv; auto — по locale клиента (ADR-0025)
  default_language: ru        # язык текстов ниже; язык диалога при auto без поддерживаемого locale
  greeting: "Привет! Помогу подобрать..."
  starter_suggestions: ["Подобрать подарок", "Условия доставки"]
  fallback_message: "Извините, сейчас не получается ответить. Попробуйте ещё раз."
  confirm_labels: { confirm: "Подтвердить", cancel: "Отмена" }   # кнопки confirm (по умолчанию)
  translations:               # тексты на других языках диалога; нет поля — базовое значение
    en: { greeting: "Hi! I can help you choose...", starter_suggestions: ["Find a gift"],
          fallback_message: "Sorry, something went wrong.", confirm_labels: { confirm: "Send" },
          # тексты компонентов инструментов — по ключам из forms и knowledge.catalog (P6-04d);
          # value вариантов select и action_id не переводятся
          forms: { contact: { title: "Contact", fields: { phone: { label: "Phone" },
                                                          time: { options: { evening: "Evening" } } } } },
          card_actions: { ask_about: "Details" }, attribute_labels: { brand: "Brand" } }
model:
  primary: { provider: openai, name: "<model>" }
  fallback: { provider: anthropic, name: "<model>" }
  # provider — протокол API, а не производитель модели (ADR-0010): openai (Responses API),
  # openai_compatible (Chat Completions совместимого API: прокси, локальные серверы),
  # anthropic (Messages API).
  # temperature (0–2) — необязательная подсказка: адаптер передаёт её, только если провайдер
  # и модель её поддерживают, иначе отбрасывает (ADR-0009). Без неё — значение провайдера.
limits: { max_steps: 6, max_tool_calls_per_step: 4, turn_timeout_s: 60, max_tool_retries: 2 }
# Сводка ранней истории (architecture.md §7): история после прошлой сводки длиннее порога
# (приблизительные токены, ~3 символа на токен) — старая часть сворачивается, последние
# keep_recent_turns ходов остаются целиком. summary_model: null — model.primary.
memory: { summary_threshold_tokens: 6000, keep_recent_turns: 4, summary_model: null }
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
  builtin: [search_knowledge, search_catalog, get_entity, show_entities, update_dialog_state,
            suggest_replies]
  custom: [ ...HTTP-инструменты... ]
forms:
  contact: { title: "...", fields: [...] }
knowledge:
  search_knowledge: { top_k: 6, rerank: true }
  catalog:
    entity_types: { product: "товары", service: "услуги" }   # filters.type search_catalog — только эти
    filterable_attributes: [color, size, material]
    attribute_labels: { color: "Цвет", size: "Размер" }   # строки comparison в show_entities
    card_actions: [{ action_id: ask_about, label: "Подробнее", style: secondary }]  # ≤ 3
branding:
  # Токены → CSS-переменные чата (apps/web/src/lib/branding.ts), форматы строгие (схема):
  # primary — #RGB/#RRGGBB (цвет текста на нём выбирается по яркости), radius — 0 или
  # число с px/rem/em, font — имя семейства (веб-шрифты не загружаются, иначе системный).
  tokens: { primary: "#1F4FFF", radius: "12px", font: "Inter" }
  logo_url: "https://…"   # или путь от корня веб-приложения ("/demo/logo.svg"); шапка чата
```

## 6. Коды ошибок (`error.code`)

`llm_unavailable` (retryable), `turn_timeout` (retryable), `step_limit`, `invalid_input`,
`conversation_not_found`, `rate_limited` (retryable), `internal`.

Только в HTTP-ответах (не в SSE `error`): `unauthorized`, `forbidden`, `not_found`,
`duplicate_message`, `not_retryable`.

## 7. API админки (P7-02, P8-01b; ADR-0028, ADR-0036)

### Вход и сессия

```
POST /v1/admin/auth/login  AdminLoginRequest {email, password}   # admin_auth.schema.json
  → 200 AdminLoginResponse {token, expires_at, me: AdminMe}
  → 401 unauthorized — неверный email или пароль, пользователь отключён (причина не уточняется)
  → 429 rate_limited + Retry-After — RATE_ADMIN_LOGIN_PER_IP_PER_MIN / _PER_EMAIL_PER_HOUR
POST /v1/admin/auth/logout → 204   # закрывает сессию из Authorization
GET  /v1/admin/me          → 200 AdminMe {id, email, is_superadmin, memberships[]}
```

Остальные запросы `/v1/admin/*` несут `Authorization: Bearer <token>`. Токен непрозрачный,
сервер хранит только его sha256; срок — `ADMIN_SESSION_TTL_HOURS` (12 ч) от входа, без продления.
Нет сессии, она истекла или закрыта, пользователь отключён — 401 `unauthorized`. Пути
`/v1/admin/tenants/{tenant_id}/…` требуют роли в этом тенанте (`viewer` < `editor`,
суперадмин — все тенанты), иначе 403 `forbidden`. Роли и отключение читаются на каждом
запросе — снятие роли действует сразу. Пользователей создаёт `make admin-user`.

### Журнал хода — роль `viewer`

```
GET /v1/admin/tenants/{tenant_id}/events?conversation_id=…&turn_id=…&trace_id=…
  → 200 AgentEventList {events: AgentEventItem[]}   # admin_events.schema.json
  → 422 invalid_input — не задан ни один фильтр
```

Фильтры применяются вместе; события — в порядке ходов (`ts`) и внутри хода (`seq`), не больше
1000. `trace_id` — `X-Trace-Id` ответа, в котором шёл ход (заголовок запроса или сгенерированный).
Ход пишет журнал вместе с ответом ассистента (одна транзакция): ход без записанного ответа
в журнале не виден.

`payload` по `type`:

| type | payload |
|---|---|
| `turn_started` | `input` (UserInput), `agent_config_id`, `retry_of` (id заменённого ответа или null) |
| `text` | `block_id`, `text` — блок целиком, на месте его начала |
| `tool_started` | `tool_call_id`, `name` |
| `tool_finished` | `tool_call_id`, `name`, `ok`, `arguments`, `duration_ms`, `error` {code, message} при ошибке; результат — в ToolCall ответа |
| `state_updated` | `state` — полное состояние диалога после шага |
| `component` | `block_id`, `component` |
| `suggestions` | `items` — подписи быстрых ответов |
| `turn_completed` | `finish`, `steps`, `usage` {input_tokens, output_tokens} — нет у прерванного и неудачного хода |
| `turn_finished` | `status` (completed/interrupted/failed), `message_id`, `error` {code, message, retryable} или null — последнее событие, есть всегда |
