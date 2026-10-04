import { expect, test } from "@playwright/test";
import type { Event } from "@/contracts";
import { type ChatState, TYPING, chatReducer, initialChatState } from "../src/lib/chat/state";

const base = { protocol_version: "1", conversation_id: "c", turn_id: "t1", ts: "" } as const;
let seq = 0;
const ev = (e: Omit<Event, keyof typeof base | "seq">): Event =>
  ({ ...base, seq: ++seq, ...e }) as Event;

function run(state: ChatState, ...events: Event[]): ChatState {
  return events.reduce((s, event) => chatReducer(s, { type: "event", event }), state);
}

const sent = chatReducer(initialChatState, { type: "user_sent", id: "local:1", text: "привет" });

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
  });
  expect(broken).toMatchObject({ busy: false, activity: null });
  expect(broken.messages.at(-1)).toMatchObject({ status: "failed", error: "нет связи" });

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
  const failed = chatReducer(sent, { type: "stream_failed", message: "неизвестный ключ виджета" });
  expect(failed).toMatchObject({ busy: false, activity: null });
  expect(failed.messages).toEqual([
    expect.objectContaining({ role: "user", status: "completed", error: "неизвестный ключ виджета" }),
  ]);
});
