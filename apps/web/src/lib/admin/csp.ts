// CSP страниц админки (ADR-0044, P9-01v): скрипты — только свои и с nonce запроса, встраивать
// во фрейм нельзя. Сессия уже в HttpOnly-cookie; CSP не даёт внедрённому скрипту действовать
// от имени админа, пока страница открыта.

/** Случайный nonce запроса (base64, 128 бит). */
export function newNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...bytes));
}

/**
 * Значение `Content-Security-Policy` для `/admin/*`. `apiOrigin` — origin API админки
 * (в проде совпадает с `'self'`, ADR-0040). `isDev` — `next dev` без `'unsafe-eval'` не
 * работает (React debugging, HMR), а его оверлей вставляет `<style>` без nonce: в dev стили
 * `'unsafe-inline'` (с nonce рядом браузер его игнорирует, поэтому без nonce).
 */
export function adminCsp(nonce: string, apiOrigin: string | null, isDev: boolean): string {
  const connect = ["'self'", ...(apiOrigin ? [apiOrigin] : [])];
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    isDev ? "style-src 'self' 'unsafe-inline'" : `style-src 'self' 'nonce-${nonce}'`,
    `connect-src ${[...new Set(connect)].join(" ")}`,
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

/** Origin из адреса API; невалидный адрес — null (останется только `'self'`). */
export function originOf(url: string | undefined): string | null {
  if (!url) return null;
  try {
    const { origin } = new URL(url);
    return origin === "null" ? null : origin;
  } catch {
    return null;
  }
}
