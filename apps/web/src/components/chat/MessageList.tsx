"use client";

import { useEffect, useRef } from "react";
import type { ChatMessage } from "@/lib/chat/state";
import { MessageBlocks } from "./MessageBlocks";

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
            className={m.role === "user" ? "self-end max-w-[85%]" : "flex w-full flex-col gap-2"}
          >
            {m.role === "user" ? (
              <div className="rounded-chat bg-chat-primary px-4 py-2 text-chat-on-primary whitespace-pre-wrap">
                {m.text}
              </div>
            ) : (
              <MessageBlocks blocks={m.blocks} />
            )}
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
