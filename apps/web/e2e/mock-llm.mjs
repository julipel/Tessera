// Мок OpenAI для e2e: Responses API (provider openai, демо-тенант; API ходит сюда через
// OPENAI_BASE_URL) и Chat Completions (provider openai_compatible).
// Отвечает стримом «Вы написали: <последнее сообщение пользователя>» по словам — так e2e
// детерминирован и проходит весь путь: агентный цикл, адаптер OpenAI, SSE, история.
// Responses API: на сообщение со словом «варианты» дополнительно вызывает suggest_replies,
// а следующий шаг (после результата инструмента) завершает пустым ответом — быстрые ответы e2e.
import { createServer } from "node:http";

const [host, port] = [process.env.MOCK_LLM_HOST, Number(process.env.MOCK_LLM_PORT)];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function chunk(delta, finishReason = null, usage = undefined) {
  const choices = delta === null ? [] : [{ index: 0, delta, finish_reason: finishReason }];
  const body = { id: "chatcmpl-e2e", object: "chat.completion.chunk", created: 0, model: "e2e", choices };
  if (usage) body.usage = usage;
  return `data: ${JSON.stringify(body)}\n\n`;
}

async function readJson(req) {
  let raw = "";
  for await (const part of req) raw += part;
  return JSON.parse(raw);
}

const lastUserText = (messages) => messages.filter((m) => m.role === "user").at(-1)?.content ?? "";

async function completions(req, res) {
  const last = lastUserText((await readJson(req)).messages);

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

function event(body) {
  return `event: ${body.type}\ndata: ${JSON.stringify(body)}\n\n`;
}

const SUGGEST_TRIGGER = "варианты";
const SUGGESTIONS = ["Сухая", "Жирная"];

async function responses(req, res) {
  const input = (await readJson(req)).input;
  const last = lastUserText(input);
  const afterTool = input.at(-1)?.type === "function_call_output";
  const response = { id: "resp_e2e", object: "response", model: "e2e", output: [] };
  let sequence = 0;
  const send = (body) => res.write(event({ ...body, sequence_number: sequence++ }));

  res.writeHead(200, { "content-type": "text/event-stream" });
  send({ type: "response.created", response: { ...response, status: "in_progress" } });
  const words = afterTool ? [] : `Вы написали: ${last}`.match(/\S+\s*/g);
  for (const word of words) {
    await sleep(30);
    send({ type: "response.output_text.delta", item_id: "msg_e2e", output_index: 0, content_index: 0, delta: word });
  }
  if (!afterTool && last.includes(SUGGEST_TRIGGER)) {
    const call = {
      type: "function_call",
      id: "fc_e2e",
      call_id: "call_e2e",
      name: "suggest_replies",
      arguments: JSON.stringify({ options: SUGGESTIONS }),
      status: "completed",
    };
    send({ type: "response.output_item.added", output_index: 1, item: { ...call, arguments: "" } });
    send({ type: "response.output_item.done", output_index: 1, item: call });
  }
  const usage = { input_tokens: 10, output_tokens: 5, total_tokens: 15 };
  res.end(event({ type: "response.completed", sequence_number: sequence, response: { ...response, status: "completed", usage } }));
}

const routes = { "/v1/chat/completions": completions, "/v1/responses": responses };

createServer((req, res) => {
  const route = req.method === "POST" ? routes[req.url] : undefined;
  if (route) {
    route(req, res).catch((error) => {
      console.error(error);
      res.destroy();
    });
  } else if (req.method === "GET" && req.url === "/health") {
    res.end("ok");
  } else {
    res.writeHead(404).end();
  }
}).listen(port, host);
