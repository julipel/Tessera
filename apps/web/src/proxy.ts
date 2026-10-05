import { type NextRequest, NextResponse } from "next/server";
import { fetchAllowedOrigins, frameAncestors } from "@/lib/widget/csp";

// Страница iframe виджета: CSP frame-ancestors по allowed_origins ключа (ADR-0022).
// Предков фрейма проверяет браузер — подделать их со встраивающей страницы нельзя.
export async function proxy(request: NextRequest) {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  const origins = await fetchAllowedOrigins(apiUrl, request.nextUrl.searchParams.get("key"));
  const response = NextResponse.next();
  response.headers.set("Content-Security-Policy", frameAncestors(origins));
  return response;
}

export const config = {
  matcher: "/widget",
};
