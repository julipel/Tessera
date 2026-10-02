// Разбор SSE-стрима хода (docs/contracts.md §2). EventSource не подходит: ход начинается POST-ом.
import type { Event } from "@/contracts";

export interface SseMessage {
  event: string;
  data: string;
}

const EVENT_TYPES: ReadonlySet<string> = new Set<Event["type"]>([
  "turn_started",
  "text_delta",
  "text_done",
  "status",
  "tool_started",
  "tool_finished",
  "component",
  "suggestions",
  "error",
  "done",
]);

/** Сообщения text/event-stream по правилам WHATWG; незавершённое в конце потока отбрасывается. */
export async function* parseSse(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SseMessage> {
  const reader = stream.getReader();
  // stream: true — многобайтный символ на границе чанков дособирается со следующим куском.
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "";
  let data: string[] = [];
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      for (;;) {
        const match = /\r\n|\r|\n/.exec(buffer);
        // `\r` в конце буфера может оказаться половиной `\r\n` — ждём следующий кусок.
        if (!match || (match[0] === "\r" && match.index === buffer.length - 1)) break;
        const line = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        if (line === "") {
          if (data.length > 0) yield { event: event || "message", data: data.join("\n") };
          event = "";
          data = [];
          continue;
        }
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        let fieldValue = colon < 0 ? "" : line.slice(colon + 1);
        if (fieldValue.startsWith(" ")) fieldValue = fieldValue.slice(1);
        if (field === "event") event = fieldValue;
        else if (field === "data") data.push(fieldValue);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
}

/** События хода; неизвестные `type` пропускаются (правило протокола). */
export async function* readEvents(stream: ReadableStream<Uint8Array>): AsyncGenerator<Event> {
  for await (const message of parseSse(stream)) {
    const parsed: unknown = JSON.parse(message.data);
    if (isEvent(parsed)) yield parsed;
  }
}

function isEvent(value: unknown): value is Event {
  return (
    typeof value === "object" &&
    value !== null &&
    "type" in value &&
    typeof value.type === "string" &&
    EVENT_TYPES.has(value.type)
  );
}
