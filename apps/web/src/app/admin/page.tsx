"use client";

import { AdminShell } from "@/components/admin/AdminShell";
import { TenantList } from "@/components/admin/TenantList";

// Админка (P8): вход — /admin/login, сессия — HttpOnly-cookie API (ADR-0044), CSP — proxy.ts.
export default function Admin() {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return <AdminShell apiUrl={apiUrl}>{({ me }) => <TenantList me={me} />}</AdminShell>;
}
