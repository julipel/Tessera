// Состояние чата: история + применение событий хода. Чистые функции — тестируются без браузера.
import type {
  Event,
  HistoryMessage,
  MessageBlock,
  MessageHistory,
  SuggestionItem,
  UserInput,
} from "@/contracts";

export type MessageStatus = "streaming" | HistoryMessage["status"];

export interface ChatMessage {
  id: string;
  role: HistoryMessage["role"];
  status: MessageStatus;
  /** Текст пользователя (для user); у assistant — пусто, содержимое в blocks. */
  text: string;
  /** Исходный ввод (для user) — его отправляет заново «Повторить», если ответа нет. */
  input?: UserInput;
  blocks: MessageBlock[];
  /** Быстрые ответы хода (событие `suggestions`); показываются только под последним ответом. */
  suggestions?: SuggestionItem[];
  turnId?: string;
  error?: string;
  /** Ошибку можно повторить: сеть, `retryable` в `error` (стрим, история) или HTTP-ответе. */
  retryable?: boolean;
}

export interface ChatState {
  messages: ChatMessage[];
  /** Ход идёт: от отправки до `done` или сбоя стрима. */
  busy: boolean;
  /** Индикатор: `TYPING` до первого текста (подпись — на языке интерфейса), иначе метка
   * `status` или инструмента от бэкенда. */
  activity: Activity | null;
  /** Идёт загрузка сохранённого диалога: приветствие и стартовые подсказки не показываются. */
  restoring: boolean;
}

export type ChatAction =
  | { type: "history"; history: MessageHistory }
  | { type: "restoring"; value: boolean }
  | { type: "user_sent"; id: string; input: UserInput }
  | { type: "retry" }
  | { type: "event"; event: Event }
  | { type: "stream_failed"; message: string; retryable: boolean };

/** «Печатает» — подпись выбирает интерфейс по языку диалога (lib/i18n.ts). */
export const TYPING = Symbol("typing");

export type Activity = typeof TYPING | string;

export const initialChatState: ChatState = {
  messages: [],
  busy: false,
  activity: null,
  restoring: false,
};

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "history":
      // Загрузка и восстановление после обрыва стрима: история — источник истины, ход окончен.
      return {
        busy: false,
        activity: null,
        restoring: false,
        messages: action.history.messages.map(fromHistory),
      };
    case "restoring":
      return { ...state, restoring: action.value };
    case "user_sent":
      return {
        ...state,
        busy: true,
        activity: TYPING,
        messages: [
          ...state.messages,
          {
            id: action.id,
            role: "user",
            status: "completed",
            text: inputText(action.input),
            input: action.input,
            blocks: [],
          },
        ],
      };
    case "retry": {
      const target = retryTarget(state.messages);
      if (!target) return state;
      const messages = state.messages.slice(0, -1);
      // Неудачный ответ заменяет новый ход по тому же вводу (ADR-0023): ввод остаётся, ход
      // начат. Ответа нет — убирается и ввод: `send` отправит его заново.
      return target.kind === "answer" ? { ...state, messages, busy: true, activity: TYPING } : { ...state, messages };
    }
    case "event":
      return applyEvent(state, action.event);
    case "stream_failed": {
      // Ответ ассистента ещё не начат (401/404 до turn_started) — ошибка под сообщением посетителя.
      const streaming = state.messages.some((m) => m.status === "streaming");
      const last = state.messages.length - 1;
      const { message: error, retryable } = action;
      return {
        ...state,
        busy: false,
        activity: null,
        messages: state.messages.map((m, i) =>
          m.status === "streaming"
            ? { ...m, status: "failed", error, retryable }
            : !streaming && i === last
              ? { ...m, error, retryable }
              : m,
        ),
      };
    }
  }
}

function applyEvent(state: ChatState, event: Event): ChatState {
  if (event.type === "turn_started") {
    const message: ChatMessage = {
      id: event.message_id ?? `turn:${event.turn_id}`,
      role: "assistant",
      status: "streaming",
      text: "",
      blocks: [],
      turnId: event.turn_id,
    };
    return { ...state, busy: true, activity: TYPING, messages: [...state.messages, message] };
  }
  const update = (fn: (m: ChatMessage) => ChatMessage): ChatMessage[] =>
    state.messages.map((m) =>
      m.turnId === event.turn_id ? fn(event.message_id ? { ...m, id: event.message_id } : m) : m,
    );
  switch (event.type) {
    case "text_delta": {
      const { block_id, delta } = event.data;
      return {
        ...state,
        activity: null,
        messages: update((m) => ({ ...m, blocks: appendText(m.blocks, block_id, delta) })),
      };
    }
    case "component": {
      const block: MessageBlock = { type: "component", ...event.data };
      return { ...state, messages: update((m) => ({ ...m, blocks: upsertBlock(m.blocks, block) })) };
    }
    case "status":
      return { ...state, activity: event.data.label };
    case "tool_started":
      // Инструмент без подписи не меняет индикатор: имя инструмента посетителю не показываем.
      return event.data.display_label
        ? { ...state, activity: `${event.data.display_label}…` }
        : state;
    case "tool_finished":
      // Дальше модель продолжает ответ.
      return { ...state, activity: TYPING };
    case "suggestions":
      // Несколько событий за ход — действует последнее.
      return { ...state, messages: update((m) => ({ ...m, suggestions: event.data.items })) };
    case "error":
      return {
        ...state,
        messages: update((m) => ({ ...m, error: event.data.message, retryable: event.data.retryable })),
      };
    case "done":
      return {
        ...state,
        busy: false,
        activity: null,
        messages: update((m) => ({ ...m, status: event.data.status })),
      };
    default:
      // text_done: блок уже собран из дельт.
      return state;
  }
}

function appendText(blocks: MessageBlock[], blockId: string, delta: string): MessageBlock[] {
  const index = blocks.findIndex((b) => b.block_id === blockId);
  if (index < 0) return [...blocks, { type: "text", block_id: blockId, text: delta }];
  return blocks.map((b, i) => (i === index && b.type === "text" ? { ...b, text: b.text + delta } : b));
}

/** Блоки упорядочены по первому появлению block_id; повтор заменяет блок на месте. */
function upsertBlock(blocks: MessageBlock[], block: MessageBlock): MessageBlock[] {
  const index = blocks.findIndex((b) => b.block_id === block.block_id);
  if (index < 0) return [...blocks, block];
  return blocks.map((b, i) => (i === index ? block : b));
}

export type RetryTarget = { kind: "answer"; messageId: string } | { kind: "resend"; input: UserInput };

/**
 * Что повторяет «Повторить» после ошибки, которую можно повторить:
 * неудачный ответ — по его id на сервере (POST …/retry); ошибка под вводом (ответа нет,
 * сбой до `turn_started`) — повторная отправка ввода; иначе null.
 */
export function retryTarget(messages: ChatMessage[]): RetryTarget | null {
  const last = messages.at(-1);
  if (!last?.retryable) return null;
  if (last.role === "user") return last.input ? { kind: "resend", input: last.input } : null;
  // Временный id `turn:…` — сервер id ответа не прислал, повторять нечего.
  if (last.status !== "failed" || last.id.startsWith("turn:")) return null;
  return { kind: "answer", messageId: last.id };
}

function fromHistory(message: HistoryMessage): ChatMessage {
  return {
    id: message.message_id,
    role: message.role,
    status: message.status,
    text: message.input ? inputText(message.input) : "",
    input: message.input,
    blocks: message.blocks,
    error: message.error?.message,
    retryable: message.error?.retryable,
  };
}

export function inputText(input: UserInput): string {
  switch (input.type) {
    case "text":
      return input.text;
    case "action":
      return input.label ?? input.action_id;
    case "form_submit":
      // Без заголовка формы подпись «Форма отправлена» выбирает интерфейс по языку.
      return input.label ?? "";
  }
}
