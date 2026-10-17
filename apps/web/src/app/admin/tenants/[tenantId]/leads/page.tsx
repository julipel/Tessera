"use client";

import { use } from "react";
import { AdminShell } from "@/components/admin/AdminShell";
import { LeadList } from "@/components/admin/LeadList";

export default function TenantLeads({ params }: PageProps<"/admin/tenants/[tenantId]/leads">) {
  const { tenantId } = use(params);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return (
    <AdminShell apiUrl={apiUrl}>
      {(session) => <LeadList key={tenantId} session={session} tenantId={tenantId} />}
    </AdminShell>
  );
}
