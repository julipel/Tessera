"use client";

import { type FormEvent, type KeyboardEvent, useState } from "react";

export function Composer({ disabled, onSend }: { disabled: boolean; onSend: (text: string) => void }) {
  const [text, setText] = useState("");

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (disabled || !text.trim()) return;
    onSend(text.trim());
    setText("");
  };

  // Enter — отправить, Shift+Enter — перенос строки.
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e);
  };

  return (
    <form
      onSubmit={submit}
      className="border-t border-chat-border bg-chat-surface px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]"
    >
      <div className="mx-auto flex max-w-2xl items-end gap-2">
        <label htmlFor="chat-input" className="sr-only">
          Сообщение
        </label>
        <textarea
          id="chat-input"
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Напишите сообщение…"
          className="max-h-40 min-h-11 flex-1 resize-none rounded-chat border border-chat-border bg-chat-surface px-3 py-2 text-base focus:outline-2 focus:outline-chat-primary"
        />
        <button
          type="submit"
          disabled={disabled || !text.trim()}
          className="min-h-11 rounded-chat bg-chat-primary px-4 font-medium text-chat-on-primary disabled:opacity-50"
        >
          Отправить
        </button>
      </div>
    </form>
  );
}
