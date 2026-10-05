"use client";

import { useEffect, useRef } from "react";
import type { UserInput } from "@/contracts";
import type { ChatMessage } from "@/lib/chat/state";
import type { OnAction } from "../rich/ActionButton";
import type { OnSubmitForm } from "../rich/Forms";
import { MessageBlocks } from "./MessageBlocks";
import { type Suggestion, Suggestions } from "./Suggestions";

const STATUS_NOTE: Partial<Record<ChatMessage["status"], string>> = {
  interrupted: "Ответ прерван",
  failed: "Не удалось ответить",
};

/**
 * `onAction`, `onSubmitForm` и `onPick` не заданы, пока идёт ход: кнопки компонентов и отправка
 * форм отключены, подсказки скрыты.
 * Подсказки — под последним ответом ассистента; на пустом чате — приветствие тенанта (`greeting`)
 * и стартовые подсказки (`starter`). Приветствие только показывается: в историю оно не попадает.
 */
export function MessageList({
  messages,
  activity,
  greeting,
  starter,
  onAction,
  onSubmitForm,
  onPick,
}: {
  messages: ChatMessage[];
  activity: string | null;
  greeting?: string;
  starter: string[];
  onAction?: OnAction;
  onSubmitForm?: OnSubmitForm;
  onPick?: (input: UserInput) => void;
}) {
  const end = useRef<HTMLDivElement>(null);
  const last = messages.at(-1);
  const suggestions: Suggestion[] = !messages.length
    ? starter.map((text) => ({ label: text, input: { type: "text", text } }))
    : last?.role === "assistant"
      ? (last.suggestions ?? [])
      : [];
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages, activity, greeting, starter]);

  return (
    <div className="flex-1 overflow-y-auto px-4 py-6">
      {!messages.length && greeting && (
        <p
          data-testid="greeting"
          className="mx-auto mb-3 max-w-2xl rounded-chat border border-chat-border bg-chat-surface px-4 py-3 whitespace-pre-wrap"
        >
          {greeting}
        </p>
      )}
      <ol className="mx-auto flex max-w-2xl flex-col gap-3" aria-label="Сообщения">
        {messages.map((m) => (
          <li
            key={m.id}
            data-role={m.role}
            data-status={m.status}
            className={m.role === "user" ? "self-end max-w-[85%]" : "flex w-full flex-col gap-2"}
          >
            {m.role === "user" ? (
              <div className="rounded-chat bg-chat-primary px-4 py-2 text-chat-on-primary whitespace-pre-wrap">
                {m.text}
              </div>
            ) : (
              <MessageBlocks blocks={m.blocks} onAction={onAction} onSubmitForm={onSubmitForm} />
            )}
            {(m.error || STATUS_NOTE[m.status]) && (
              <p className="mt-1 text-sm text-chat-danger">{m.error ?? STATUS_NOTE[m.status]}</p>
            )}
          </li>
        ))}
      </ol>
      {onPick && (
        <div className="mx-auto mt-3 max-w-2xl">
          <Suggestions items={suggestions} onPick={onPick} />
        </div>
      )}
      <div aria-live="polite" className="mx-auto max-w-2xl">
        {activity && (
          <p role="status" className="mt-3 text-sm text-chat-muted animate-pulse">
            {activity}
          </p>
        )}
      </div>
      <div ref={end} />
    </div>
  );
}
