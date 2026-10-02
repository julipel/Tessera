"use client";

import { useEffect, useRef } from "react";
import type { ChatMessage } from "@/lib/chat/state";

const STATUS_NOTE: Partial<Record<ChatMessage["status"], string>> = {
  interrupted: "Ответ прерван",
  failed: "Не удалось ответить",
};

export function MessageList({
  messages,
  activity,
}: {
  messages: ChatMessage[];
  activity: string | null;
}) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages, activity]);

  return (
    <div className="flex-1 overflow-y-auto px-4 py-6">
      <ol className="mx-auto flex max-w-2xl flex-col gap-3" aria-label="Сообщения">
        {messages.map((m) => (
          <li
            key={m.id}
            data-role={m.role}
            data-status={m.status}
            className={m.role === "user" ? "self-end max-w-[85%]" : "self-start max-w-[85%]"}
          >
            <div
              className={
                m.role === "user"
                  ? "rounded-chat bg-chat-primary px-4 py-2 text-chat-on-primary whitespace-pre-wrap"
                  : "rounded-chat border border-chat-border bg-chat-surface px-4 py-2 whitespace-pre-wrap"
              }
            >
              {m.role === "user"
                ? m.text
                : // Компоненты (P5-02) пока не рендерятся; markdown — тоже с P5.
                  m.blocks.map((b) => (b.type === "text" ? <p key={b.block_id}>{b.text}</p> : null))}
            </div>
            {(m.error || STATUS_NOTE[m.status]) && (
              <p className="mt-1 text-sm text-chat-danger">{m.error ?? STATUS_NOTE[m.status]}</p>
            )}
          </li>
        ))}
      </ol>
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
