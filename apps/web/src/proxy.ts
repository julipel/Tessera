import { type NextRequest, NextResponse } from "next/server";
import { adminCsp, newNonce, originOf } from "@/lib/admin/csp";
import { fetchAllowedOrigins, frameAncestors } from "@/lib/widget/csp";

export async function proxy(request: NextRequest) {
  return request.nextUrl.pathname.startsWith("/admin") ? admin(request) : widget(request);
}

// Страница iframe виджета: CSP frame-ancestors по allowed_origins ключа (ADR-0022).
// Предков фрейма проверяет браузер — подделать их со встраивающей страницы нельзя.
async function widget(request: NextRequest) {
  // API_INTERNAL_URL — адрес API из сети сервера (compose, ADR-0040): запрос не выходит
  // наружу и не зависит от DNS/TLS публичного домена. NEXT_PUBLIC_* вшит в сборку.
  const apiUrl =
    process.env.API_INTERNAL_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  const origins = await fetchAllowedOrigins(apiUrl, request.nextUrl.searchParams.get("key"));
  const response = NextResponse.next();
  response.headers.set("Content-Security-Policy", frameAncestors(origins));
  return response;
}

// Админка: nonce-CSP (P9-01v, ADR-0044). CSP в заголовках запроса — чтобы Next при рендере
// взял nonce и расставил его своим скриптам; страницы админки рендерятся на каждый запрос
// (app/admin/layout.tsx).
function admin(request: NextRequest) {
  const csp = adminCsp(
    newNonce(),
    originOf(process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"),
    process.env.NODE_ENV === "development",
  );
  const headers = new Headers(request.headers);
  headers.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: ["/widget", "/admin/:path*"],
};
