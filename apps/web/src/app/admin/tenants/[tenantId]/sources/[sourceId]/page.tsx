"use client";

import { use } from "react";
import { AdminShell } from "@/components/admin/AdminShell";
import { SourceView } from "@/components/admin/SourceView";

export default function TenantSource({ params }: PageProps<"/admin/tenants/[tenantId]/sources/[sourceId]">) {
  const { tenantId, sourceId } = use(params);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return (
    <AdminShell apiUrl={apiUrl}>
      {(session) => <SourceView key={sourceId} session={session} tenantId={tenantId} sourceId={sourceId} />}
    </AdminShell>
  );
}
