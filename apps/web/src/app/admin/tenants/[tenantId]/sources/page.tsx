"use client";

import { use } from "react";
import { AdminShell } from "@/components/admin/AdminShell";
import { SourceList } from "@/components/admin/SourceList";

export default function TenantSources({ params }: PageProps<"/admin/tenants/[tenantId]/sources">) {
  const { tenantId } = use(params);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return (
    <AdminShell apiUrl={apiUrl}>
      {(session) => <SourceList key={tenantId} session={session} tenantId={tenantId} />}
    </AdminShell>
  );
}
