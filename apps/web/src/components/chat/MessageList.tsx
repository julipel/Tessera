"use client";

import { useEffect, useRef, useState } from "react";
import type { UserInput } from "@/contracts";
import { type Activity, type ChatMessage, TYPING } from "@/lib/chat/state";
import { type Messages, useI18n } from "@/lib/i18n";
import type { OnAction } from "../rich/ActionButton";
import type { OnSubmitForm } from "../rich/Forms";
import { MessageBlocks } from "./MessageBlocks";
import { type Suggestion, Suggestions } from "./Suggestions";

const STATUS_NOTE: Partial<Record<ChatMessage["status"], keyof Messages>> = {
  interrupted: "interrupted",
  failed: "failed",
};

// Ближе этого к низу ленты — считаем, что посетитель внизу и следит за ответом.
const STICK_PX = 80;

/**
 * `onAction`, `onSubmitForm` и `onPick` не заданы, пока идёт ход: кнопки компонентов и отправка
 * форм отключены, подсказки скрыты.
 * Подсказки — под последним ответом ассистента; на пустом чате — приветствие тенанта (`greeting`)
 * и стартовые подсказки (`starter`). Приветствие только показывается: в историю оно не попадает.
 * `onRetry` задан, если последний ввод можно повторить, — кнопка под последним сообщением.
 *
 * Автоскролл: лента едет за ответом, только пока посетитель внизу; прокрутил вверх — лента
 * стоит, появляется кнопка «К новым сообщениям». Своя отправка всегда возвращает вниз.
 */
export function MessageList({
  messages,
  activity,
  greeting,
  starter,
  onAction,
  onSubmitForm,
  onPick,
  onRetry,
}: {
  messages: ChatMessage[];
  activity: Activity | null;
  greeting?: string;
  starter: string[];
  onAction?: OnAction;
  onSubmitForm?: OnSubmitForm;
  onPick?: (input: UserInput) => void;
  onRetry?: () => void;
}) {
  const { t } = useI18n();
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const lastUser = useRef<string | undefined>(undefined);
  const last = messages.at(-1);
  const suggestions: Suggestion[] = !messages.length
    ? starter.map((text) => ({ label: text, input: { type: "text", text } }))
    : last?.role === "assistant"
      ? (last.suggestions ?? [])
      : [];

  const scrollToEnd = () => {
    const el = scroller.current;
    // Не scrollIntoView: во фрейме виджета он прокрутил бы и страницу сайта.
    if (el) el.scrollTop = el.scrollHeight;
  };
  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_PX;
    setAtBottom(stick.current);
  };
  useEffect(() => {
    const userId = messages.findLast((m) => m.role === "user")?.id;
    if (userId !== lastUser.current) {
      lastUser.current = userId;
      stick.current = true;
    }
    if (stick.current) scrollToEnd();
  }, [messages, activity, greeting, starter, onRetry]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={scroller} onScroll={onScroll} className="flex-1 overflow-y-auto px-4 py-6">
        {!messages.length && greeting && (
          <p
            data-testid="greeting"
            className="mx-auto mb-3 max-w-2xl rounded-chat border border-chat-border bg-chat-surface px-4 py-3 whitespace-pre-wrap"
          >
            {greeting}
          </p>
        )}
        <ol className="mx-auto flex max-w-2xl flex-col gap-3" aria-label={t.messagesLabel}>
          {messages.map((m) => (
            <li
              key={m.id}
              data-role={m.role}
              data-status={m.status}
              className={m.role === "user" ? "self-end max-w-[85%]" : "flex w-full flex-col gap-2"}
            >
              {m.role === "user" ? (
                <div className="rounded-chat bg-chat-primary px-4 py-2 text-chat-on-primary whitespace-pre-wrap">
                  {m.text || (m.input?.type === "form_submit" ? t.formSent : "")}
                </div>
              ) : (
                <MessageBlocks blocks={m.blocks} onAction={onAction} onSubmitForm={onSubmitForm} />
              )}
              {(m.error || STATUS_NOTE[m.status]) && (
                <p className="mt-1 text-sm text-chat-danger">{m.error ?? t[STATUS_NOTE[m.status]!]}</p>
              )}
              {onRetry && m === last && (
                <button
                  type="button"
                  onClick={onRetry}
                  className="mt-1 self-start rounded-chat border border-chat-border bg-chat-surface px-3 py-1 text-sm hover:bg-chat-bg focus:outline-2 focus:outline-chat-primary"
                >
                  {t.retry}
                </button>
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
              {activity === TYPING ? t.typing : activity}
            </p>
          )}
        </div>
      </div>
      {!atBottom && (
        <button
          type="button"
          onClick={scrollToEnd}
          aria-label={t.toNewMessages}
          title={t.toNewMessages}
          className="absolute bottom-3 left-1/2 flex size-10 -translate-x-1/2 items-center justify-center rounded-full border border-chat-border bg-chat-surface text-chat-muted shadow hover:bg-chat-bg focus:outline-2 focus:outline-chat-primary"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true" className="size-5 fill-none stroke-current stroke-2">
            <path d="M12 5v14M6 13l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      )}
    </div>
  );
}
