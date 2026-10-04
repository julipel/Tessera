import type { UserInput } from "@/contracts";

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
  if (!items.length) return null;
  return (
    <div role="group" aria-label="Быстрые ответы" className="flex flex-wrap gap-2">
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          onClick={() => onPick(item.input)}
          className="min-h-9 rounded-full border border-chat-primary bg-chat-surface px-3 text-sm text-chat-primary hover:bg-chat-bg"
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
