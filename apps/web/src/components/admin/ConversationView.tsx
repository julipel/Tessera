"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type {
  AdminMessage,
  AdminToolCall,
  AgentEventItem,
  ConversationDetail,
  MessageRating,
  UserInput,
} from "@/contracts";
import { ApiError } from "@/lib/api/client";
import type { AdminSession } from "@/lib/admin/useAdminSession";
import { secondaryButton } from "./AdminShell";
import { RatingCounts } from "./ConversationList";
import { type Problem, ProblemView, toProblem } from "./ProblemView";
import { formatTime } from "./sourceLabels";

const STATUS_LABELS: Record<AdminMessage["status"], string> = {
  completed: "готов",
  interrupted: "прерван",
  failed: "ошибка",
};

const EVENT_LABELS: Record<AgentEventItem["type"], string> = {
  turn_started: "начало хода",
  text: "текст",
  tool_started: "вызов инструмента",
  tool_finished: "инструмент",
  component: "компонент",
  suggestions: "быстрые ответы",
  state_updated: "состояние",
  turn_completed: "ход завершён",
  turn_finished: "итог",
};

/**
 * Разбор диалога: сообщения (и заменённые повтором), ход по шагам из журнала (ADR-0028)
 * под каждым ответом и оценка ответа (ADR-0038).
 */
export function ConversationView({
  session,
  tenantId,
  conversationId,
}: {
  session: AdminSession;
  tenantId: string;
  conversationId: string;
}) {
  const { api, expire } = session;
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [turns, setTurns] = useState<Map<string, AgentEventItem[]>>(new Map());
  const [problem, setProblem] = useState<Problem | null>(null);

  const fail = useCallback(
    (e: unknown) => (e instanceof ApiError && e.status === 401 ? expire() : setProblem(toProblem(e))),
    [expire],
  );

  useEffect(() => {
    Promise.all([api.getConversation(tenantId, conversationId), api.listEvents(tenantId, conversationId)])
      .then(([conversation, { events }]) => {
        setDetail(conversation);
        setTurns(turnsByMessage(events));
      })
      .catch(fail);
  }, [api, tenantId, conversationId, fail]);

  function rated(messageId: string, rating: MessageRating | null) {
    setDetail((prev) => prev && withRating(prev, messageId, rating));
  }

  const back = (
    <Link href={`/admin/tenants/${tenantId}/conversations`} className="text-sm text-chat-primary hover:underline">
      ← Диалоги
    </Link>
  );
  if (!detail) {
    return (
      <div className="space-y-4">
        {back}
        {problem ? <ProblemView problem={problem} title="Ошибка" /> : <p className="text-chat-muted">Загрузка…</p>}
      </div>
    );
  }
  const c = detail.conversation;
  return (
    <div className="space-y-6">
      <div>
        {back}
        <h1 className="mt-1 text-xl font-semibold">Диалог от {formatTime(c.created_at)}</h1>
        <p className="text-sm text-chat-muted">
          {c.id} · язык {c.language ?? "—"} · конфиг v{c.agent_config_version} · сообщений {c.messages} ·{" "}
          <RatingCounts good={c.ratings.good} bad={c.ratings.bad} />
        </p>
      </div>
      {problem && <ProblemView problem={problem} title="Ошибка" />}
      <details className="rounded-chat border border-chat-border bg-chat-surface p-3 text-sm">
        <summary className="cursor-pointer font-medium">Состояние и сводка</summary>
        <p className="mt-2 text-chat-muted">Сводка ранней истории:</p>
        <p className="whitespace-pre-wrap">{detail.history_summary ?? "—"}</p>
        <p className="mt-2 text-chat-muted">Состояние диалога:</p>
        <Json value={detail.state} />
      </details>
      {detail.messages.length === 0 ? (
        <p className="text-chat-muted">Сообщений нет.</p>
      ) : (
        <ol aria-label="Сообщения" className="space-y-4">
          {detail.messages.map((m) => (
            <li key={m.message_id} data-role={m.role}>
              {m.role === "user" ? (
                <UserMessage message={m} />
              ) : (
                <AssistantMessage
                  message={m}
                  events={turns.get(m.message_id) ?? []}
                  onRate={async (request) => {
                    try {
                      const rating = request
                        ? await api.rateMessage(tenantId, conversationId, m.message_id, request)
                        : (await api.clearRating(tenantId, conversationId, m.message_id), null);
                      rated(m.message_id, rating);
                    } catch (e) {
                      fail(e);
                      throw e;
                    }
                  }}
                />
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function UserMessage({ message }: { message: AdminMessage }) {
  return (
    <div className="ml-auto max-w-[80%] rounded-chat bg-chat-primary/10 px-4 py-2">
      <div className="text-xs text-chat-muted">Клиент · {formatTime(message.created_at)}</div>
      <div className="whitespace-pre-wrap">{message.input ? inputText(message.input) : "—"}</div>
    </div>
  );
}

type RateRequest = { rating: "good" | "bad"; comment: string | null } | null;

function AssistantMessage({
  message,
  events,
  onRate,
}: {
  message: AdminMessage;
  events: AgentEventItem[];
  onRate: (request: RateRequest) => Promise<void>;
}) {
  const replaced = message.replaced_by !== null;
  return (
    <article
      aria-label={`Ответ ${formatTime(message.created_at)}`}
      className={`max-w-[90%] rounded-chat border border-chat-border bg-chat-surface px-4 py-3 ${replaced ? "opacity-60" : ""}`}
    >
      <div className="text-xs text-chat-muted">
        Ассистент · {formatTime(message.created_at)}
        {message.status !== "completed" && (
          <span className={message.status === "failed" ? " text-chat-danger" : ""}> · {STATUS_LABELS[message.status]}</span>
        )}
        {replaced && <span> · заменён повтором</span>}
      </div>
      {message.blocks.map((b) =>
        b.type === "text" ? (
          <p key={b.block_id} className="whitespace-pre-wrap">
            {b.text}
          </p>
        ) : (
          <details key={b.block_id} className="my-1 text-sm">
            <summary className="cursor-pointer text-chat-muted">Компонент: {b.component.type}</summary>
            <Json value={b.component} />
          </details>
        ),
      )}
      {message.error && (
        <p className="text-sm text-chat-danger">
          {message.error.code}: {message.error.message}
        </p>
      )}
      <Steps events={events} toolCalls={message.tool_calls} />
      {!replaced && <RatingControl rating={message.rating} onRate={onRate} />}
    </article>
  );
}

/** Ход по шагам: события журнала; результат инструмента — из ToolCall ответа. */
function Steps({ events, toolCalls }: { events: AgentEventItem[]; toolCalls: AdminToolCall[] }) {
  if (events.length === 0 && toolCalls.length === 0) return null;
  const results = new Map(toolCalls.map((c) => [c.tool_call_id, c]));
  return (
    <details className="mt-2 text-sm">
      <summary className="cursor-pointer text-chat-primary">Ход по шагам ({events.length})</summary>
      {events.length > 0 && <p className="mt-1 text-xs text-chat-muted">trace_id: {events[0].trace_id}</p>}
      {events.length === 0 && <p className="mt-1 text-chat-muted">Журнала хода нет.</p>}
      <ol aria-label="Шаги хода" className="mt-1 space-y-1">
        {events.map((e) => (
          <li key={e.seq} className="border-l-2 border-chat-border pl-3">
            <span className="text-chat-muted">{e.seq}.</span> <span className="font-medium">{EVENT_LABELS[e.type]}</span>{" "}
            <EventBody event={e} results={results} />
          </li>
        ))}
      </ol>
      {events.length === 0 &&
        toolCalls.map((c) => (
          <div key={c.tool_call_id} className="border-l-2 border-chat-border pl-3">
            <ToolCallView call={c} />
          </div>
        ))}
    </details>
  );
}

function EventBody({ event, results }: { event: AgentEventItem; results: Map<string, AdminToolCall> }) {
  const p = event.payload as Record<string, unknown>;
  switch (event.type) {
    case "turn_started":
      return <span className="text-chat-muted">{p.retry_of ? "повтор ответа" : ""}</span>;
    case "text":
      return <span className="whitespace-pre-wrap text-chat-muted">{String(p.text ?? "")}</span>;
    case "tool_started":
      return <code>{String(p.name)}</code>;
    case "tool_finished": {
      const call = results.get(String(p.tool_call_id));
      return call ? (
        <ToolCallView call={call} />
      ) : (
        <>
          <code>{String(p.name)}</code> · {p.ok ? "ok" : "ошибка"} · {String(p.duration_ms)} мс
          <Json value={{ arguments: p.arguments, error: p.error }} />
        </>
      );
    }
    case "component":
      return <span className="text-chat-muted">{String((p.component as { type?: string } | undefined)?.type)}</span>;
    case "suggestions":
      return <span className="text-chat-muted">{(p.items as string[] | undefined)?.join(" · ")}</span>;
    case "state_updated":
      return (
        <details className="inline">
          <summary className="inline cursor-pointer text-chat-muted">показать</summary>
          <Json value={p.state} />
        </details>
      );
    case "turn_completed": {
      const usage = p.usage as { input_tokens?: number; output_tokens?: number } | undefined;
      return (
        <span className="text-chat-muted">
          {String(p.finish)} · шагов {String(p.steps)} · токены {usage?.input_tokens ?? "?"}/{usage?.output_tokens ?? "?"}
        </span>
      );
    }
    case "turn_finished": {
      const error = p.error as { code?: string; message?: string } | null;
      return (
        <span className={error ? "text-chat-danger" : "text-chat-muted"}>
          {String(p.status)}
          {error && ` · ${error.code}: ${error.message}`}
        </span>
      );
    }
  }
}

function ToolCallView({ call }: { call: AdminToolCall }) {
  return (
    <>
      <code>{call.name}</code> · {call.error ? <span className="text-chat-danger">ошибка</span> : "ok"} ·{" "}
      {call.duration_ms} мс
      <details>
        <summary className="cursor-pointer text-chat-muted">аргументы и результат</summary>
        <Json value={call.error ? { arguments: call.arguments, error: call.error } : { arguments: call.arguments, result: call.result }} />
      </details>
    </>
  );
}

/** 👍/👎 сохраняются сразу вместе с комментарием; комментарий можно дописать позже. */
function RatingControl({
  rating,
  onRate,
}: {
  rating: MessageRating | null;
  onRate: (request: RateRequest) => Promise<void>;
}) {
  const [comment, setComment] = useState(rating?.comment ?? "");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  async function run(request: RateRequest, done: string) {
    setBusy(true);
    setSaved(null);
    try {
      await onRate(request);
      if (!request) setComment("");
      setSaved(done);
    } catch {
      // ошибку показывает страница
    } finally {
      setBusy(false);
    }
  }

  const text = comment.trim() || null;
  return (
    <div className="mt-3 space-y-2 border-t border-chat-border pt-2 text-sm">
      <div role="group" aria-label="Оценка ответа" className="flex flex-wrap items-center gap-2">
        {(["good", "bad"] as const).map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={rating?.rating === value}
            disabled={busy}
            onClick={() => void run({ rating: value, comment: text }, "Оценка сохранена")}
            className={`${secondaryButton} aria-pressed:border-chat-primary aria-pressed:font-semibold`}
          >
            {value === "good" ? "👍 Хорошо" : "👎 Плохо"}
          </button>
        ))}
        {rating && (
          <button type="button" disabled={busy} onClick={() => void run(null, "Оценка снята")} className={secondaryButton}>
            Снять оценку
          </button>
        )}
        {rating?.rated_by && (
          <span className="text-xs text-chat-muted">
            {rating.rated_by}, {formatTime(rating.updated_at)}
          </span>
        )}
        {saved && (
          <span role="status" className="text-xs text-chat-muted">
            {saved}
          </span>
        )}
      </div>
      <label className="block">
        <span className="text-chat-muted">Комментарий к оценке</span>
        <textarea
          value={comment}
          maxLength={2000}
          onChange={(e) => setComment(e.target.value)}
          className="mt-1 block h-16 w-full rounded-chat border border-chat-border bg-chat-bg px-3 py-2"
        />
      </label>
      {rating && text !== rating.comment && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void run({ rating: rating.rating, comment: text }, "Комментарий сохранён")}
          className={secondaryButton}
        >
          Сохранить комментарий
        </button>
      )}
    </div>
  );
}

function Json({ value }: { value: unknown }) {
  return (
    <pre className="mt-1 max-h-80 overflow-auto rounded-chat bg-chat-bg p-2 text-xs">{JSON.stringify(value, null, 2)}</pre>
  );
}

function inputText(input: UserInput): string {
  if (input.type === "text") return input.text;
  if (input.type === "action") return `[кнопка] ${input.label ?? input.action_id}`;
  return `[форма ${input.form_id}] ${JSON.stringify(input.values)}`;
}

/** События журнала по id ответа: ход ↔ ответ связывает `turn_finished.message_id`. */
function turnsByMessage(events: AgentEventItem[]): Map<string, AgentEventItem[]> {
  const byTurn = new Map<string, AgentEventItem[]>();
  for (const e of events) byTurn.set(e.turn_id, [...(byTurn.get(e.turn_id) ?? []), e]);
  const byMessage = new Map<string, AgentEventItem[]>();
  for (const turn of byTurn.values()) {
    const finished = turn.find((e) => e.type === "turn_finished");
    const messageId = (finished?.payload as { message_id?: string } | undefined)?.message_id;
    if (messageId) byMessage.set(messageId, turn);
  }
  return byMessage;
}

function withRating(detail: ConversationDetail, messageId: string, rating: MessageRating | null): ConversationDetail {
  const messages = detail.messages.map((m) => (m.message_id === messageId ? { ...m, rating } : m));
  const count = (value: "good" | "bad") => messages.filter((m) => m.rating?.rating === value).length;
  return {
    ...detail,
    messages,
    conversation: { ...detail.conversation, ratings: { good: count("good"), bad: count("bad") } },
  };
}
