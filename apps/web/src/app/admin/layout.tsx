import { connection } from "next/server";
import type { ReactNode } from "react";

// Страницы админки рендерятся на каждый запрос: nonce из CSP (proxy.ts) Next расставляет
// только при рендере на сервере, в статической сборке его нет (P9-01v).
export default async function AdminLayout({ children }: { children: ReactNode }) {
  await connection();
  return children;
}
