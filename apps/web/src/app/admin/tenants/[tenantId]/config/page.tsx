"use client";

import { use } from "react";
import { AdminShell } from "@/components/admin/AdminShell";
import { ConfigEditor } from "@/components/admin/ConfigEditor";

export default function TenantConfig({ params }: PageProps<"/admin/tenants/[tenantId]/config">) {
  const { tenantId } = use(params);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return (
    <AdminShell apiUrl={apiUrl}>
      {(session) => <ConfigEditor key={tenantId} session={session} tenantId={tenantId} />}
    </AdminShell>
  );
}
