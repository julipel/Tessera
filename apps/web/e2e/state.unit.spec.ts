import { expect, test } from "@playwright/test";
import type { Event } from "@/contracts";
import {
  type ChatState,
  TYPING,
  chatReducer,
  initialChatState,
  inputText,
  retryTarget,
} from "../src/lib/chat/state";

const base = { protocol_version: "1", conversation_id: "c", turn_id: "t1", ts: "" } as const;
let seq = 0;
const ev = (e: Omit<Event, keyof typeof base | "seq">): Event =>
  ({ ...base, seq: ++seq, ...e }) as Event;

function run(state: ChatState, ...events: Event[]): ChatState {
  return events.reduce((s, event) => chatReducer(s, { type: "event", event }), state);
}

const sent = chatReducer(initialChatState, {
  type: "user_sent",
  id: "local:1",
  input: { type: "text", text: "привет" },
});

test("ход: индикатор до первого текста, дельты склеиваются по block_id, done завершает", () => {
  expect(sent).toMatchObject({ busy: true, activity: TYPING });

  const started = run(sent, ev({ type: "turn_started", data: {} }));
  expect(started.messages.at(-1)).toMatchObject({ role: "assistant", status: "streaming" });
  expect(started.activity).toBe(TYPING);

  const streaming = run(
    started,
    ev({ type: "text_delta", message_id: "m1", data: { block_id: "b1", delta: "Вы " } }),
    ev({ type: "text_delta", message_id: "m1", data: { block_id: "b1", delta: "написали" } }),
  );
  expect(streaming.activity).toBeNull();
  expect(streaming.messages.at(-1)).toMatchObject({
    id: "m1",
    blocks: [{ type: "text", block_id: "b1", text: "Вы написали" }],
  });

  const done = run(
    streaming,
    ev({ type: "text_done", data: { block_id: "b1" } }),
    ev({ type: "done", data: { status: "completed" } }),
  );
  expect(done).toMatchObject({ busy: false, activity: null });
  expect(done.messages.at(-1)?.status).toBe("completed");
});

test("component добавляет блок после текста; повтор block_id заменяет блок на месте", () => {
  const card = (title: string) =>
    ({ type: "product_card", entity_id: "e1", title }) as const;
  const state = run(
    sent,
    ev({ type: "turn_started", data: {} }),
    ev({ type: "text_delta", data: { block_id: "b1", delta: "Вот:" } }),
    ev({ type: "component", data: { block_id: "b2", component: card("Крем") } }),
    ev({ type: "component", data: { block_id: "b2", component: card("Крем SPF") } }),
  );
  expect(state.messages.at(-1)?.blocks).toEqual([
    { type: "text", block_id: "b1", text: "Вот:" },
    { type: "component", block_id: "b2", component: card("Крем SPF") },
  ]);
});

test("status меняет подпись индикатора, error попадает в сообщение", () => {
  const state = run(
    sent,
    ev({ type: "turn_started", data: {} }),
    ev({ type: "status", data: { kind: "searching", label: "Ищу в каталоге…" } }),
  );
  expect(state.activity).toBe("Ищу в каталоге…");

  const failed = run(
    state,
    ev({ type: "error", data: { code: "internal", message: "сбой", retryable: false } }),
    ev({ type: "done", data: { status: "failed" } }),
  );
  expect(failed.messages.at(-1)).toMatchObject({ status: "failed", error: "сбой" });
});

test("сбой стрима помечает незавершённый ответ, история заменяет состояние", () => {
  const broken = chatReducer(run(sent, ev({ type: "turn_started", data: {} })), {
    type: "stream_failed",
    message: "нет связи",
    retryable: true,
  });
  expect(broken).toMatchObject({ busy: false, activity: null });
  expect(broken.messages.at(-1)).toMatchObject({
    status: "failed",
    error: "нет связи",
    retryable: true,
  });

  const restored = chatReducer(broken, {
    type: "history",
    history: {
      conversation_id: "c",
      messages: [
        {
          message_id: "u1",
          role: "user",
          status: "completed",
          created_at: "",
          input: { type: "text", text: "привет" },
          blocks: [],
        },
        {
          message_id: "m1",
          role: "assistant",
          status: "interrupted",
          created_at: "",
          blocks: [{ type: "text", block_id: "b1", text: "Вы на" }],
        },
      ],
    },
  });
  expect(restored.messages.map((m) => [m.role, m.status, m.text])).toEqual([
    ["user", "completed", "привет"],
    ["assistant", "interrupted", ""],
  ]);
});

test("ошибка до начала ответа (например, 401) показывается под сообщением посетителя", () => {
  const failed = chatReducer(sent, {
    type: "stream_failed",
    message: "неизвестный ключ виджета",
    retryable: false,
  });
  expect(failed).toMatchObject({ busy: false, activity: null });
  expect(failed.messages).toEqual([
    expect.objectContaining({
      role: "user",
      status: "completed",
      error: "неизвестный ключ виджета",
      retryable: false,
    }),
  ]);
});

test("нажатие кнопки: в истории подпись выбора, без подписи — action_id", () => {
  const action = {
    type: "action",
    action_id: "ask_about",
    label: "Подробнее — Крем",
    payload: { entity_id: "e1" },
  } as const;
  const state = chatReducer(initialChatState, { type: "user_sent", id: "local:2", input: action });
  expect(state.messages).toEqual([
    expect.objectContaining({ role: "user", text: "Подробнее — Крем" }),
  ]);
  expect(state.busy).toBe(true);

  // Старые сообщения истории — без label.
  expect(inputText({ type: "action", action_id: "select_product" })).toBe("select_product");
});

test("отправка формы: в истории заголовок формы, без подписи — общая надпись", () => {
  const submit = {
    type: "form_submit",
    form_id: "consultation",
    label: "Консультация косметолога",
    values: { name: "Анна", contact: "@anna" },
  } as const;
  const state = chatReducer(initialChatState, { type: "user_sent", id: "local:3", input: submit });
  // Значения формы в подпись не попадают.
  expect(state.messages).toEqual([
    expect.objectContaining({ role: "user", text: "Консультация косметолога" }),
  ]);
  expect(state.busy).toBe(true);

  // Без заголовка формы подпись выбирает интерфейс по языку (MessageList, lib/i18n.ts).
  expect(inputText({ type: "form_submit", form_id: "f", values: {} })).toBe("");
});

test("suggestions: подсказки у сообщения хода, действует последнее событие", () => {
  const item = (text: string) => ({ label: text, input: { type: "text", text } }) as const;
  const state = run(
    sent,
    ev({ type: "turn_started", message_id: "m2", data: {} }),
    ev({ type: "suggestions", data: { items: [item("Сухая")] } }),
    ev({ type: "suggestions", data: { items: [item("Жирная"), item("Не знаю")] } }),
    ev({ type: "done", data: { status: "completed" } }),
  );
  expect(state.messages.at(-1)).toMatchObject({
    role: "assistant",
    suggestions: [item("Жирная"), item("Не знаю")],
  });
  // Подсказки остаются у своего сообщения; MessageList показывает их только под последним.
  const next = chatReducer(state, { type: "user_sent", id: "local:3", input: item("Жирная").input });
  expect(next.messages.at(-1)).toMatchObject({ role: "user", text: "Жирная" });
});

test("история: ввод action показывается подписью", () => {
  const restored = chatReducer(initialChatState, {
    type: "history",
    history: {
      conversation_id: "c",
      messages: [
        {
          message_id: "u1",
          role: "user",
          status: "completed",
          created_at: "",
          input: { type: "action", action_id: "ask_about", label: "Подробнее — Сыворотка" },
          blocks: [],
        },
      ],
    },
  });
  expect(restored.messages[0].text).toBe("Подробнее — Сыворотка");
});

test("инструменты: подпись display_label в индикаторе, без подписи индикатор не меняется", () => {
  const started = run(sent, ev({ type: "turn_started", data: {} }));
  const tool = (name: string, label: string | null) =>
    ev({ type: "tool_started", data: { tool_call_id: name, name, display_label: label } });

  const searching = run(started, tool("search_catalog", "Ищу в каталоге"));
  expect(searching.activity).toBe("Ищу в каталоге…");
  expect(run(searching, tool("suggest_replies", null)).activity).toBe("Ищу в каталоге…");

  const finished = run(
    searching,
    ev({ type: "tool_finished", data: { tool_call_id: "search_catalog", ok: true, duration_ms: 5 } }),
  );
  expect(finished.activity).toBe(TYPING);
  expect(finished.busy).toBe(true);
});

test("повтор: неудачный ответ повторяется по id, retry убирает только ответ и начинает ход", () => {
  const failed = run(
    sent,
    ev({ type: "turn_started", data: {} }),
    ev({
      type: "error",
      message_id: "a1",
      data: { code: "llm_unavailable", message: "недоступна", retryable: true },
    }),
    ev({ type: "done", message_id: "a1", data: { status: "failed" } }),
  );
  expect(failed.messages.map((m) => [m.role, m.status, m.retryable])).toEqual([
    ["user", "completed", undefined],
    ["assistant", "failed", true],
  ]);
  expect(retryTarget(failed.messages)).toEqual({ kind: "answer", messageId: "a1" });

  const retried = chatReducer(failed, { type: "retry" });
  expect(retried.messages).toEqual(failed.messages.slice(0, 1));
  expect([retried.busy, retried.activity]).toEqual([true, TYPING]);
});

test("повтор: ошибка до ответа — повторная отправка ввода", () => {
  const rejected = chatReducer(sent, { type: "stream_failed", message: "сбой", retryable: true });
  expect(retryTarget(rejected.messages)).toEqual({ kind: "resend", input: { type: "text", text: "привет" } });

  const retried = chatReducer(rejected, { type: "retry" });
  expect(retried.messages).toEqual([]);
  expect(retried.busy).toBe(false); // ход начнёт повторная отправка
});

test("повтор: нечего повторять — неповторяемая ошибка, успешный ответ, нет id ответа", () => {
  const notRetryable = chatReducer(sent, { type: "stream_failed", message: "нет", retryable: false });
  const answered = run(
    sent,
    ev({ type: "turn_started", data: {} }),
    ev({ type: "done", data: { status: "completed" } }),
  );
  // Сервер не прислал id ответа — остался временный `turn:…`.
  const noId: ChatState = {
    ...sent,
    busy: false,
    messages: [
      ...sent.messages,
      { id: "turn:t1", role: "assistant", status: "failed", text: "", blocks: [], retryable: true },
    ],
  };

  for (const state of [notRetryable, answered, noId]) {
    expect(retryTarget(state.messages)).toBeNull();
    expect(chatReducer(state, { type: "retry" })).toBe(state);
  }
});

test("повтор после перезагрузки: error из истории — текст и retryable", () => {
  const restored = chatReducer(initialChatState, {
    type: "history",
    history: {
      conversation_id: "c",
      messages: [
        { message_id: "u1", role: "user", status: "completed", created_at: "", input: { type: "text", text: "a" }, blocks: [] },
        {
          message_id: "a1",
          role: "assistant",
          status: "failed",
          created_at: "",
          blocks: [],
          error: { code: "llm_unavailable", message: "модель сейчас недоступна", retryable: true },
        },
      ],
    },
  });
  expect(restored.messages[1]).toMatchObject({ error: "модель сейчас недоступна", retryable: true });
  expect(retryTarget(restored.messages)).toEqual({ kind: "answer", messageId: "a1" });
});

test("восстановление: restoring до загрузки истории, ввод из истории доступен для повтора", () => {
  const restoring = chatReducer(initialChatState, { type: "restoring", value: true });
  expect(restoring.restoring).toBe(true);

  const input = { type: "text", text: "привет" } as const;
  const restored = chatReducer(restoring, {
    type: "history",
    history: {
      conversation_id: "c",
      messages: [
        { message_id: "u1", role: "user", status: "completed", created_at: "", input, blocks: [] },
      ],
    },
  });
  expect(restored.restoring).toBe(false);
  expect(restored.messages[0].input).toEqual(input);
});
