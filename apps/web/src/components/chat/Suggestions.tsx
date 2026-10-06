"use client";

import type { UserInput } from "@/contracts";
import { useI18n } from "@/lib/i18n";

export interface Suggestion {
  label: string;
  input: UserInput;
}

/** Быстрые ответы: нажатие отправляет `input` как обычный ввод посетителя. */
export function Suggestions({
  items,
  onPick,
}: {
  items: Suggestion[];
  onPick: (input: UserInput) => void;
}) {
  const { t } = useI18n();
  if (!items.length) return null;
  return (
    <div role="group" aria-label={t.suggestionsLabel} className="flex flex-wrap gap-2">
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          onClick={() => onPick(item.input)}
          className="min-h-9 rounded-chat border border-chat-primary bg-chat-surface px-3 text-sm text-chat-primary hover:bg-chat-bg"
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
