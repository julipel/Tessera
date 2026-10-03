// Мок OpenAI-совместимого Chat Completions для e2e: API ходит сюда через OPENAI_BASE_URL.
// Отвечает стримом «Вы написали: <последнее сообщение пользователя>» по словам — так e2e
// детерминирован и проходит весь путь: агентный цикл, адаптер OpenAI, SSE, история.
import { createServer } from "node:http";

const [host, port] = [process.env.MOCK_LLM_HOST, Number(process.env.MOCK_LLM_PORT)];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function chunk(delta, finishReason = null, usage = undefined) {
  const choices = delta === null ? [] : [{ index: 0, delta, finish_reason: finishReason }];
  const body = { id: "chatcmpl-e2e", object: "chat.completion.chunk", created: 0, model: "e2e", choices };
  if (usage) body.usage = usage;
  return `data: ${JSON.stringify(body)}\n\n`;
}

async function completions(req, res) {
  let raw = "";
  for await (const part of req) raw += part;
  const { messages } = JSON.parse(raw);
  const last = messages.filter((m) => m.role === "user").at(-1)?.content ?? "";

  res.writeHead(200, { "content-type": "text/event-stream" });
  res.write(chunk({ role: "assistant", content: "" }));
  // Пауза между словами — чтобы e2e успел увидеть заблокированную отправку во время хода.
  for (const word of `Вы написали: ${last}`.match(/\S+\s*/g)) {
    await sleep(30);
    res.write(chunk({ content: word }));
  }
  res.write(chunk({}, "stop"));
  res.write(chunk(null, null, { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 }));
  res.end("data: [DONE]\n\n");
}

createServer((req, res) => {
  if (req.method === "POST" && req.url === "/v1/chat/completions") {
    completions(req, res).catch((error) => {
      console.error(error);
      res.destroy();
    });
  } else if (req.method === "GET" && req.url === "/health") {
    res.end("ok");
  } else {
    res.writeHead(404).end();
  }
}).listen(port, host);
