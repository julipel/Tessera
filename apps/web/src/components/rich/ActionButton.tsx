import type { Action } from "@/contracts";

/** `subject` — к чему относится кнопка (название позиции карточки): уходит в подпись выбора. */
export type OnAction = (action: Action, subject?: string) => void;

const STYLE: Record<NonNullable<Action["style"]>, string> = {
  primary: "bg-chat-primary text-chat-on-primary",
  secondary: "border border-chat-border bg-chat-surface",
  link: "text-chat-primary underline underline-offset-2 px-0",
};

/** Action с url — ссылка; без url — кнопка, отправляющая input.type=action. Без обработчика
 * (идёт ход, витрина компонентов) кнопка отключена. */
export function ActionButton({
  action,
  subject,
  onAction,
}: {
  action: Action;
  subject?: string;
  onAction?: OnAction;
}) {
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
      onClick={onAction && (() => onAction(action, subject))}
    >
      {action.label}
    </button>
  );
}

export function ActionRow({
  actions,
  subject,
  onAction,
}: {
  actions?: Action[];
  subject?: string;
  onAction?: OnAction;
}) {
  if (!actions?.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {actions.map((a) => (
        <ActionButton key={a.action_id} action={a} subject={subject} onAction={onAction} />
      ))}
    </div>
  );
}
