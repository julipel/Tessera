import { expect, test } from "@playwright/test";
import { parseSse, readEvents } from "../src/lib/api/sse";

function streamOf(...chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(typeof c === "string" ? encoder.encode(c) : c);
      controller.close();
    },
  });
}

async function collect<T>(gen: AsyncGenerator<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const item of gen) out.push(item);
  return out;
}

const envelope = (type: string, data: object) =>
  JSON.stringify({ protocol_version: "1", seq: 1, type, conversation_id: "c", turn_id: "t", ts: "", data });

test("сообщения, разорванные посреди строки, CRLF и комментарии", async () => {
  const messages = await collect(
    parseSse(streamOf("event: a\r", "\ndata: 1\r\n\r\n: ping\n\nev", "ent: b\ndata: x\ndata: y\n\n")),
  );
  expect(messages).toEqual([
    { event: "a", data: "1" },
    { event: "b", data: "x\ny" },
  ]);
});

test("многобайтный UTF-8 на границе чанков и незавершённое сообщение в конце", async () => {
  const bytes = new TextEncoder().encode("data: привет\n\ndata: обрыв");
  const messages = await collect(parseSse(streamOf(bytes.slice(0, 9), bytes.slice(9))));
  expect(messages).toEqual([{ event: "message", data: "привет" }]);
});

test("неизвестные типы событий пропускаются", async () => {
  const body =
    `event: future\ndata: ${envelope("future", {})}\n\n` +
    `event: text_delta\ndata: ${envelope("text_delta", { block_id: "b1", delta: "Привет" })}\n\n`;
  const events = await collect(readEvents(streamOf(body)));
  expect(events.map((e) => e.type)).toEqual(["text_delta"]);
});
