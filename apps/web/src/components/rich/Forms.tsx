// form и confirm: пока только вёрстка; отправка формы и подтверждение — P5-04.
// Кнопка отправки отключена — неявная отправка по Enter тоже не срабатывает.
import type { Confirm, Form, FormField } from "@/contracts";
import { ActionButton, type OnAction } from "./ActionButton";

const INPUT_TYPE: Record<Exclude<FormField["kind"], "textarea" | "select">, string> = {
  text: "text",
  phone: "tel",
  email: "email",
  date: "date",
};

const CONTROL =
  "min-h-10 w-full rounded-chat border border-chat-border bg-chat-surface px-3 py-2 text-base focus:outline-2 focus:outline-chat-primary";

function Field({ formId, field }: { formId: string; field: FormField }) {
  const id = `${formId}-${field.name}`;
  const common = { id, name: field.name, required: field.required, className: CONTROL };
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm">
        {field.label}
        {field.required && <span aria-hidden className="text-chat-danger"> *</span>}
      </label>
      {field.kind === "textarea" ? (
        <textarea rows={3} {...common} />
      ) : field.kind === "select" ? (
        <select defaultValue="" {...common}>
          <option value="" disabled>
            Выберите…
          </option>
          {field.options?.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      ) : (
        <input type={INPUT_TYPE[field.kind]} {...common} />
      )}
    </div>
  );
}

export function FormView({ data }: { data: Form }) {
  return (
    <form
      aria-label={data.title}
      className="flex flex-col gap-3 rounded-chat border border-chat-border bg-chat-surface p-3"
    >
      <h3 className="font-medium">{data.title}</h3>
      {data.fields.map((f) => (
        <Field key={f.name} formId={data.form_id} field={f} />
      ))}
      <button
        type="submit"
        disabled
        className="min-h-10 rounded-chat bg-chat-primary px-4 font-medium text-chat-on-primary disabled:opacity-50"
      >
        {data.submit_label ?? "Отправить"}
      </button>
    </form>
  );
}

export function ConfirmView({ data, onAction }: { data: Confirm; onAction?: OnAction }) {
  return (
    <div role="group" aria-label="Подтверждение" className="flex flex-col gap-3 rounded-chat border border-chat-border bg-chat-surface p-3">
      <p>{data.text}</p>
      <div className="flex flex-wrap gap-2">
        <ActionButton action={{ style: "primary", ...data.confirm_action }} onAction={onAction} />
        {data.cancel_action && <ActionButton action={data.cancel_action} onAction={onAction} />}
      </div>
    </div>
  );
}
