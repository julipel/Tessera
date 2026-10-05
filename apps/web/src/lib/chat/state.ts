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
  blocks: MessageBlock[];
  /** Быстрые ответы хода (событие `suggestions`); показываются только под последним ответом. */
  suggestions?: SuggestionItem[];
  turnId?: string;
  error?: string;
}

export interface ChatState {
  messages: ChatMessage[];
  /** Ход идёт: от отправки до `done` или сбоя стрима. */
  busy: boolean;
  /** Подпись индикатора: «печатает» до первого текста или метка `status`. */
  activity: string | null;
}

export type ChatAction =
  | { type: "history"; history: MessageHistory }
  | { type: "user_sent"; id: string; input: UserInput }
  | { type: "event"; event: Event }
  | { type: "stream_failed"; message: string };

export const TYPING = "Печатает…";

export const initialChatState: ChatState = { messages: [], busy: false, activity: null };

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "history":
      // Загрузка и восстановление после обрыва стрима: история — источник истины, ход окончен.
      return { busy: false, activity: null, messages: action.history.messages.map(fromHistory) };
    case "user_sent":
      return {
        busy: true,
        activity: TYPING,
        messages: [
          ...state.messages,
          { id: action.id, role: "user", status: "completed", text: inputText(action.input), blocks: [] },
        ],
      };
    case "event":
      return applyEvent(state, action.event);
    case "stream_failed": {
      // Ответ ассистента ещё не начат (401/404 до turn_started) — ошибка под сообщением посетителя.
      const streaming = state.messages.some((m) => m.status === "streaming");
      const last = state.messages.length - 1;
      return {
        busy: false,
        activity: null,
        messages: state.messages.map((m, i) =>
          m.status === "streaming"
            ? { ...m, status: "failed", error: action.message }
            : !streaming && i === last
              ? { ...m, error: action.message }
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
    case "suggestions":
      // Несколько событий за ход — действует последнее.
      return { ...state, messages: update((m) => ({ ...m, suggestions: event.data.items })) };
    case "error":
      return { ...state, messages: update((m) => ({ ...m, error: event.data.message })) };
    case "done":
      return {
        busy: false,
        activity: null,
        messages: update((m) => ({ ...m, status: event.data.status })),
      };
    default:
      // text_done и инструменты — с P5-07.
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

function fromHistory(message: HistoryMessage): ChatMessage {
  return {
    id: message.message_id,
    role: message.role,
    status: message.status,
    text: message.input ? inputText(message.input) : "",
    blocks: message.blocks,
  };
}

export function inputText(input: UserInput): string {
  switch (input.type) {
    case "text":
      return input.text;
    case "action":
      return input.label ?? input.action_id;
    case "form_submit":
      return input.label ?? "Форма отправлена";
  }
}
