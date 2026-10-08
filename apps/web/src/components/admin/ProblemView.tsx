import type { ErrorDetail } from "@/contracts";
import { ApiError } from "@/lib/api/client";

export interface Problem {
  message: string;
  details: ErrorDetail[];
}

/** Ошибка запроса для показа: текст и ошибки по местам ввода (`details`), если есть. */
export function toProblem(e: unknown): Problem {
  const message = e instanceof Error ? e.message : String(e);
  return { message, details: (e instanceof ApiError && e.body.details) || [] };
}

/** Ошибка: с `details` — списком мест (`loc` через точку), иначе — текстом. */
export function ProblemView({ problem, title }: { problem: Problem; title: string }) {
  return (
    <div role="alert" className="rounded-chat border border-chat-danger p-3 text-sm text-chat-danger">
      {problem.details.length > 0 ? (
        <>
          <p className="font-medium">{title}:</p>
          <ul className="mt-1 list-disc pl-5">
            {problem.details.map((d, i) => (
              <li key={i}>
                {d.loc.length > 0 && <code className="mr-1">{d.loc.join(".")}</code>}
                {d.message}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p>{problem.message}</p>
      )}
    </div>
  );
}
