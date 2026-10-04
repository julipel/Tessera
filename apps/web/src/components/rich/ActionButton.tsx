import type { Action } from "@/contracts";

export type OnAction = (action: Action) => void;

const STYLE: Record<NonNullable<Action["style"]>, string> = {
  primary: "bg-chat-primary text-chat-on-primary",
  secondary: "border border-chat-border bg-chat-surface",
  link: "text-chat-primary underline underline-offset-2 px-0",
};

/** Action с url — ссылка; без url — кнопка, отправляющая input.type=action (обработчик — P5-03). */
export function ActionButton({ action, onAction }: { action: Action; onAction?: OnAction }) {
  const className = `inline-flex min-h-9 items-center justify-center rounded-chat px-3 text-sm font-medium disabled:opacity-50 ${STYLE[action.style ?? "secondary"]}`;
  if (action.url) {
    return (
      <a href={action.url} target="_blank" rel="noopener noreferrer" className={className}>
        {action.label}
      </a>
    );
  }
  return (
    <button
      type="button"
      className={className}
      disabled={!onAction}
      onClick={onAction && (() => onAction(action))}
    >
      {action.label}
    </button>
  );
}

export function ActionRow({ actions, onAction }: { actions?: Action[]; onAction?: OnAction }) {
  if (!actions?.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {actions.map((a) => (
        <ActionButton key={a.action_id} action={a} onAction={onAction} />
      ))}
    </div>
  );
}
