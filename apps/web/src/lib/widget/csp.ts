// CSP страницы iframe виджета (ADR-0022): встроить `/widget` можно только на сайтах из
// `allowed_origins` ключа. При любой ошибке — `frame-ancestors 'none'` (закрыто по умолчанию).
import type { WidgetEmbed } from "@/contracts";

// Тот же формат, что ORIGIN_PATTERN в API (tenants/domain/widget_keys.py): API уже проверил
// значения, здесь — вторая линия, чтобы в заголовок не попала своя директива.
const ORIGIN = /^https?:\/\/[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$/;

const FETCH_TIMEOUT_MS = 3000;

/** Значение `Content-Security-Policy`: разрешённые предки фрейма или `'none'`. */
export function frameAncestors(origins: unknown): string {
  const valid = Array.isArray(origins)
    ? origins.filter((o): o is string => typeof o === "string" && ORIGIN.test(o))
    : [];
  return `frame-ancestors ${valid.length ? [...new Set(valid)].join(" ") : "'none'"}`;
}

/** `allowed_origins` ключа из `GET /v1/public/widget`; при любой ошибке — пустой список. */
export async function fetchAllowedOrigins(
  apiUrl: string,
  widgetKey: string | null,
  fetchImpl: typeof fetch = fetch,
): Promise<string[]> {
  if (!widgetKey) return [];
  try {
    const resp = await fetchImpl(new URL("/v1/public/widget", apiUrl), {
      headers: { "X-Widget-Key": widgetKey },
      cache: "no-store",
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    if (!resp.ok) return [];
    const body = (await resp.json()) as Partial<WidgetEmbed> | null;
    return Array.isArray(body?.allowed_origins) ? body.allowed_origins : [];
  } catch {
    return [];
  }
}
