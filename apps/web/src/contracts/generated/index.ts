/* eslint-disable */
// Сгенерировано из packages/contracts/schemas — не редактировать, запустить `make contracts`.

/**
 * UI-компонент, отправляемый в чат инструментом бэкенда. Discriminated union по type. Все цены/URL — из данных, не от модели.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Component".
 */
export type Component =
  | ProductCard
  | ProductCarousel
  | InfoCard
  | ImageComponent
  | LinkList
  | Sources
  | ComparisonTable
  | Form
  | Confirm;
/**
 * Ввод пользователя в POST .../messages: текст, нажатие action-кнопки или отправка формы.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "UserInput".
 */
export type UserInput = TextInput | ActionInput | FormSubmitInput;
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "MessageBlock".
 */
export type MessageBlock = TextBlock | ComponentBlock;
/**
 * Полный SSE-конверт (Envelope + типизированный data) для каждого значения type. Discriminated union по type.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Event".
 */
export type Event =
  | TurnStartedEvent
  | TextDeltaEvent
  | TextDoneEvent
  | StatusEvent
  | ToolStartedEvent
  | ToolFinishedEvent
  | ComponentEvent
  | SuggestionsEvent
  | ErrorEvent
  | DoneEvent;
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "TurnStartedEvent".
 */
export type TurnStartedEvent = EnvelopeFields & {
  type: "turn_started";
  data: TurnStartedData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "TextDeltaEvent".
 */
export type TextDeltaEvent = EnvelopeFields & {
  type: "text_delta";
  data: TextDeltaData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "TextDoneEvent".
 */
export type TextDoneEvent = EnvelopeFields & {
  type: "text_done";
  data: TextDoneData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "StatusEvent".
 */
export type StatusEvent = EnvelopeFields & {
  type: "status";
  data: StatusData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ToolStartedEvent".
 */
export type ToolStartedEvent = EnvelopeFields & {
  type: "tool_started";
  data: ToolStartedData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ToolFinishedEvent".
 */
export type ToolFinishedEvent = EnvelopeFields & {
  type: "tool_finished";
  data: ToolFinishedData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ComponentEvent".
 */
export type ComponentEvent = EnvelopeFields & {
  type: "component";
  data: ComponentData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "SuggestionsEvent".
 */
export type SuggestionsEvent = EnvelopeFields & {
  type: "suggestions";
  data: SuggestionsData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ErrorEvent".
 */
export type ErrorEvent = EnvelopeFields & {
  type: "error";
  data: ErrorData;
};
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "DoneEvent".
 */
export type DoneEvent = EnvelopeFields & {
  type: "done";
  data: DoneData;
};

export interface Contracts {
  [k: string]: unknown;
}
/**
 * Конфигурация тенанта (поле config JSONB таблицы AgentConfig). Вся бизнес-специфика — здесь, не в коде ядра.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AgentConfig".
 */
export interface AgentConfig {
  assistant: AssistantConfig;
  model: ModelsConfig;
  limits: LimitsConfig;
  prompt: PromptConfig;
  tools: ToolsConfig;
  /**
   * Ключ — form_key (используется в show_form и в component.form_id).
   */
  forms?: {
    [k: string]: FormConfig;
  };
  knowledge?: KnowledgeConfig;
  branding?: BrandingConfig;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "AssistantConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "AssistantConfig".
 */
export interface AssistantConfig {
  name: string;
  language?: "auto" | "ru" | "en" | "sv";
  greeting: string;
  starter_suggestions?: string[];
  fallback_message: string;
  confirm_labels?: ConfirmLabels;
}
/**
 * Подписи кнопок компонента confirm у инструментов с requires_confirmation (ADR-0021).
 *
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "ConfirmLabels".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ConfirmLabels".
 */
export interface ConfirmLabels {
  confirm?: string;
  cancel?: string;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "ModelsConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ModelsConfig".
 */
export interface ModelsConfig {
  primary: ModelConfig;
  fallback?: ModelConfig | null;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "ModelConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ModelConfig".
 */
export interface ModelConfig {
  /**
   * Протокол API, а не производитель модели (ADR-0010): openai — OpenAI Responses API, openai_compatible — Chat Completions OpenAI-совместимого API (прокси, локальные серверы; например, модель anthropic/… через прокси), anthropic — Anthropic Messages API.
   */
  provider: "openai" | "openai_compatible" | "anthropic";
  name: string;
  /**
   * Подсказка, не требование (ADR-0009): применяется, только если провайдер и модель её поддерживают, иначе адаптер её отбрасывает. Не задана — значение провайдера по умолчанию.
   */
  temperature?: number;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "LimitsConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "LimitsConfig".
 */
export interface LimitsConfig {
  max_steps?: number;
  max_tool_calls_per_step?: number;
  turn_timeout_s?: number;
  max_tool_retries?: number;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "PromptConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "PromptConfig".
 */
export interface PromptConfig {
  /**
   * Tenant-слой промпта: роль, тон, о компании, границы (см. architecture.md §6).
   */
  tenant: string;
  scenarios?: ScenarioConfig[];
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "ScenarioConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ScenarioConfig".
 */
export interface ScenarioConfig {
  key: string;
  description: string;
  instructions: string;
  slots?: {
    [k: string]: SlotDefinition;
  };
}
/**
 * Слот сценария (DialogState). Для type=array элементы описывает items.
 *
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "SlotDefinition".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "SlotDefinition".
 */
export interface SlotDefinition {
  type: "string" | "number" | "boolean" | "array";
  description?: string;
  /**
   * Допустимые значения (для string).
   */
  enum?: string[];
  items?: SlotItems;
}
/**
 * Используется при type=array, напр. concerns[] из пилота beauty.
 */
export interface SlotItems {
  type: "string" | "number";
  enum?: string[];
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "ToolsConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ToolsConfig".
 */
export interface ToolsConfig {
  builtin?: (
    | "search_knowledge"
    | "search_catalog"
    | "get_entity"
    | "show_entities"
    | "update_dialog_state"
    | "suggest_replies"
    | "show_form"
    | "create_lead"
    | "handoff_to_human"
  )[];
  custom?: HttpToolDefinition[];
}
/**
 * Декларативный HTTP-инструмент тенанта из AgentConfig.tools.custom. Кода на тенанта не пишем.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "HttpToolDefinition".
 */
export interface HttpToolDefinition {
  name: string;
  kind: "http";
  description: string;
  parameters: {};
  request: HttpRequestConfig;
  response: HttpResponseConfig;
  timeout_s?: number;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "HttpRequestConfig".
 */
export interface HttpRequestConfig {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /**
   * URL-шаблон, плейсхолдеры — имена параметров из parameters, напр. {entity_id}.
   */
  url: string;
  auth?: HttpAuth;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "HttpAuth".
 */
export interface HttpAuth {
  type: "bearer" | "api_key" | "basic";
  /**
   * Имя переменной окружения с секретом. Секрет никогда не хранится в конфиге.
   */
  secret_ref: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "HttpResponseConfig".
 */
export interface HttpResponseConfig {
  /**
   * JMESPath-выражение, маппинг ответа API в content инструмента.
   */
  content_jmespath: string;
  /**
   * Опциональный маппинг ответа в UI-компонент (тип из components.schema.json).
   */
  component?: string | null;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "FormConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "FormConfig".
 */
export interface FormConfig {
  title: string;
  /**
   * @minItems 1
   */
  fields: [FormField, ...FormField[]];
}
/**
 * Поле формы. kind=select требует options.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "FormField".
 */
export interface FormField {
  name: string;
  label: string;
  /**
   * text — короткая строка (в т.ч. телефон/телеграм одним полем, см. пилот beauty); phone/email — со встроенной валидацией на клиенте; textarea — многострочный комментарий; select — выбор из options; date — дата.
   */
  kind: "text" | "phone" | "email" | "textarea" | "select" | "date";
  required?: boolean;
  options?: FormFieldOption[];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "FormFieldOption".
 */
export interface FormFieldOption {
  label: string;
  value: string;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "KnowledgeConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "KnowledgeConfig".
 */
export interface KnowledgeConfig {
  search_knowledge?: KnowledgeSearchConfig;
  catalog?: CatalogConfig;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "KnowledgeSearchConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "KnowledgeSearchConfig".
 */
export interface KnowledgeSearchConfig {
  top_k?: number;
  rerank?: boolean;
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "CatalogConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "CatalogConfig".
 */
export interface CatalogConfig {
  /**
   * Типы позиций каталога (Entity.type) → описание для модели; search_catalog принимает filters.type только из них.
   */
  entity_types?: {
    [k: string]: string;
  };
  /**
   * Имена полей Entity.attributes, по которым search_catalog принимает фильтры.
   */
  filterable_attributes?: string[];
  /**
   * Подписи атрибутов Entity.attributes для пользователя; show_entities(layout=comparison) выводит строки только для них, в этом порядке.
   */
  attribute_labels?: {
    [k: string]: string;
  };
  /**
   * Кнопки каждой product_card из show_entities; payload кнопки — {entity_id}.
   *
   * @maxItems 3
   */
  card_actions?:
    [] | [CardAction] | [CardAction, CardAction] | [CardAction, CardAction, CardAction];
}
/**
 * Кнопка карточки позиции: нажатие отправляет input.type=action с payload {entity_id}.
 *
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "CardAction".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "CardAction".
 */
export interface CardAction {
  action_id: string;
  label: string;
  style?: "primary" | "secondary" | "link";
}
/**
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "BrandingConfig".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "BrandingConfig".
 */
export interface BrandingConfig {
  tokens?: BrandingTokens;
  /**
   * Логотип в шапке чата: https://… или путь от корня веб-приложения.
   */
  logo_url?: string | null;
}
/**
 * Дизайн-токены чата: значения попадают в CSS-переменные, поэтому форматы строгие.
 *
 * This interface was referenced by `AgentConfig`'s JSON-Schema
 * via the `definition` "BrandingTokens".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "BrandingTokens".
 */
export interface BrandingTokens {
  /**
   * Основной цвет (кнопки, сообщения посетителя): #RGB или #RRGGBB.
   */
  primary?: string;
  /**
   * Радиус скругления: 0 или число с px / rem / em.
   */
  radius?: string;
  /**
   * Имя семейства шрифта (без загрузки: нет у посетителя — системный).
   */
  font?: string;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ProductCard".
 */
export interface ProductCard {
  type: "product_card";
  entity_id: string;
  title: string;
  subtitle?: string | null;
  image_url?: string | null;
  /**
   * Нет у позиции цены (услуга, «цена по запросу») — null.
   */
  price?: Price | null;
  badges?: string[];
  url?: string | null;
  actions?: Action[];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Price".
 */
export interface Price {
  amount: number;
  /**
   * ISO 4217, напр. RUB.
   */
  currency: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Action".
 */
export interface Action {
  action_id: string;
  label: string;
  style?: "primary" | "secondary" | "link";
  payload?: {};
  /**
   * Если задан — кнопка является ссылкой, клик не отправляет input.type=action.
   */
  url?: string | null;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ProductCarousel".
 */
export interface ProductCarousel {
  type: "product_carousel";
  title?: string | null;
  /**
   * @minItems 1
   */
  items: [ProductCard, ...ProductCard[]];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "InfoCard".
 */
export interface InfoCard {
  type: "info_card";
  title: string;
  body_markdown: string;
  image_url?: string | null;
  url?: string | null;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ImageComponent".
 */
export interface ImageComponent {
  type: "image";
  url: string;
  alt: string;
  caption?: string | null;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "LinkList".
 */
export interface LinkList {
  type: "link_list";
  /**
   * @minItems 1
   */
  items: [LinkListItem, ...LinkListItem[]];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "LinkListItem".
 */
export interface LinkListItem {
  title: string;
  url: string;
  description?: string | null;
}
/**
 * Источники из search_knowledge — объяснимость ответа.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Sources".
 */
export interface Sources {
  type: "sources";
  /**
   * @minItems 1
   */
  items: [SourceItem, ...SourceItem[]];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "SourceItem".
 */
export interface SourceItem {
  title: string;
  url: string;
  snippet?: string | null;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ComparisonTable".
 */
export interface ComparisonTable {
  type: "comparison_table";
  /**
   * @minItems 2
   */
  columns: [string, string, ...string[]];
  /**
   * @minItems 1
   */
  rows: [ComparisonTableRow, ...ComparisonTableRow[]];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ComparisonTableRow".
 */
export interface ComparisonTableRow {
  label: string;
  values: string[];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Form".
 */
export interface Form {
  type: "form";
  form_id: string;
  title: string;
  /**
   * @minItems 1
   */
  fields: [FormField, ...FormField[]];
  submit_label?: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Confirm".
 */
export interface Confirm {
  type: "confirm";
  confirm_id: string;
  text: string;
  confirm_action: Action;
  cancel_action?: Action;
}
/**
 * Тело POST /v1/conversations. visitor_id генерирует и хранит клиент.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "CreateConversationRequest".
 */
export interface CreateConversationRequest {
  visitor_id: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "CreateConversationResponse".
 */
export interface CreateConversationResponse {
  conversation_id: string;
}
/**
 * Тело POST /v1/conversations/{id}/messages; ответ — SSE-стрим (docs/contracts.md §2).
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "SendMessageRequest".
 */
export interface SendMessageRequest {
  /**
   * Ключ идемпотентности, уникален в пределах диалога: повтор → 409 duplicate_message.
   */
  client_message_id: string;
  input: UserInput;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "TextInput".
 */
export interface TextInput {
  type: "text";
  text: string;
}
/**
 * Нажатие Action-кнопки компонента (см. components.schema.json).
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ActionInput".
 */
export interface ActionInput {
  type: "action";
  action_id: string;
  /**
   * Что увидел пользователь (подпись кнопки, при необходимости с названием позиции): показывается в истории и передаётся модели. Нет — вместо подписи action_id.
   */
  label?: string;
  payload?: {};
}
/**
 * Отправка формы (component.type=form). Ключи values соответствуют FormField.name.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "FormSubmitInput".
 */
export interface FormSubmitInput {
  type: "form_submit";
  form_id: string;
  /**
   * Что увидел пользователь (заголовок формы): показывается в истории вместо form_id. Модели не передаётся — она получает form_id и values.
   */
  label?: string;
  values: {};
}
/**
 * Ответ GET /v1/conversations/{id}/messages: сообщения в порядке создания.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "MessageHistory".
 */
export interface MessageHistory {
  conversation_id: string;
  messages: HistoryMessage[];
}
/**
 * Сообщение истории. У user — исходный ввод (input), у assistant — блоки в порядке первого появления block_id в стриме.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "HistoryMessage".
 */
export interface HistoryMessage {
  message_id: string;
  role: "user" | "assistant";
  status: "completed" | "interrupted" | "failed";
  created_at: string;
  input?: UserInput;
  blocks: MessageBlock[];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "TextBlock".
 */
export interface TextBlock {
  type: "text";
  block_id: string;
  text: string;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ComponentBlock".
 */
export interface ComponentBlock {
  type: "component";
  block_id: string;
  component: Component;
}
/**
 * SSE-конверт: event: <type> / data: <Envelope JSON>. Форма data для конкретного type описана в events.schema.json.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "Envelope".
 */
export interface Envelope {
  protocol_version: "1";
  /**
   * Монотонно растёт в пределах одного хода (turn_id).
   */
  seq: number;
  type: string;
  conversation_id: string;
  turn_id: string;
  /**
   * Отсутствует у событий, предшествующих созданию сообщения ассистента (напр. turn_started).
   */
  message_id?: string | null;
  ts: string;
  data: {};
}
/**
 * Общие поля конверта, переиспользуются в каждом варианте события (см. envelope.schema.json).
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "EnvelopeFields".
 */
export interface EnvelopeFields {
  protocol_version: "1";
  seq: number;
  conversation_id: string;
  turn_id: string;
  message_id?: string | null;
  ts: string;
}
export interface TurnStartedData {}
export interface TextDeltaData {
  block_id: string;
  delta: string;
}
export interface TextDoneData {
  block_id: string;
}
export interface StatusData {
  kind: "searching" | "thinking" | "calling_api";
  label: string;
}
export interface ToolStartedData {
  tool_call_id: string;
  name: string;
  display_label?: string | null;
}
export interface ToolFinishedData {
  tool_call_id: string;
  ok: boolean;
  duration_ms: number;
}
export interface ComponentData {
  block_id: string;
  component: Component;
}
export interface SuggestionsData {
  /**
   * @minItems 1
   */
  items: [SuggestionItem, ...SuggestionItem[]];
}
export interface SuggestionItem {
  label: string;
  input: UserInput;
}
export interface ErrorData {
  code:
    | "llm_unavailable"
    | "turn_timeout"
    | "step_limit"
    | "invalid_input"
    | "conversation_not_found"
    | "rate_limited"
    | "internal";
  message: string;
  retryable: boolean;
}
export interface DoneData {
  status: "completed" | "interrupted" | "failed";
  usage?: DoneUsage;
}
export interface DoneUsage {
  input_tokens?: number;
  output_tokens?: number;
  tool_calls?: number;
  [k: string]: unknown;
}
/**
 * Тело любого HTTP-ответа с ошибкой (4xx/5xx) публичного API. Коды — docs/contracts.md §6.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "HttpError".
 */
export interface HttpError {
  error: HttpErrorBody;
}
/**
 * This interface was referenced by `HttpError`'s JSON-Schema
 * via the `definition` "HttpErrorBody".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "HttpErrorBody".
 */
export interface HttpErrorBody {
  /**
   * Коды SSE-ошибок (events.schema.json ErrorData) плюс только HTTP: unauthorized (401), forbidden (403), not_found (404), duplicate_message (409).
   */
  code:
    | "llm_unavailable"
    | "turn_timeout"
    | "step_limit"
    | "invalid_input"
    | "conversation_not_found"
    | "rate_limited"
    | "internal"
    | "unauthorized"
    | "forbidden"
    | "not_found"
    | "duplicate_message";
  message: string;
  retryable: boolean;
}
/**
 * Ответ GET /v1/public/config: то, что виджет/чат показывает до первого сообщения. Только публичная часть активного AgentConfig — без промпта, инструментов, моделей и лимитов.
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "PublicConfig".
 */
export interface PublicConfig {
  assistant: PublicAssistant;
  branding: BrandingConfig;
}
/**
 * This interface was referenced by `PublicConfig`'s JSON-Schema
 * via the `definition` "PublicAssistant".
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "PublicAssistant".
 */
export interface PublicAssistant {
  name: string;
  language: "auto" | "ru" | "en" | "sv";
  greeting: string;
  starter_suggestions: string[];
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ToolError".
 */
export interface ToolError {
  code: "validation_error" | "not_found" | "upstream_error" | "timeout" | "forbidden";
  /**
   * Понятно модели.
   */
  message: string;
  retryable: boolean;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ToolResult".
 */
export interface ToolResult {
  /**
   * Компактно, для модели.
   */
  content: string | {};
  components?: Component[];
  /**
   * Частичное обновление DialogState.
   */
  state_patch?: {} | null;
  error?: ToolError | null;
}
/**
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "ToolDefinition".
 */
export interface ToolDefinition {
  /**
   * snake_case, уникален в конфиге тенанта.
   */
  name: string;
  /**
   * Для модели: когда и зачем вызывать.
   */
  description: string;
  /**
   * JSON Schema аргументов инструмента.
   */
  parameters: {};
  timeout_s?: number;
  /**
   * Меняет что-то во внешнем мире (создаёт заявку, бронь и т.п.).
   */
  side_effect?: boolean;
  requires_confirmation?: boolean;
  /**
   * Например «Ищу в каталоге…» — для события tool_started.
   */
  display_label?: string | null;
}
/**
 * Ответ GET /v1/public/widget: где разрешено встраивать виджет. Читает сервер веб-чата, чтобы выставить CSP frame-ancestors на странице iframe (ADR-0022).
 *
 * This interface was referenced by `Contracts`'s JSON-Schema
 * via the `definition` "WidgetEmbed".
 */
export interface WidgetEmbed {
  /**
   * Origin сайтов тенанта (`scheme://host[:port]`). Пустой список — встраивание запрещено.
   */
  allowed_origins: string[];
}
