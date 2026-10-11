"use client";

import { use } from "react";
import { AdminShell } from "@/components/admin/AdminShell";
import { ConversationList } from "@/components/admin/ConversationList";

export default function TenantConversations({ params }: PageProps<"/admin/tenants/[tenantId]/conversations">) {
  const { tenantId } = use(params);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return (
    <AdminShell apiUrl={apiUrl}>
      {(session) => <ConversationList key={tenantId} session={session} tenantId={tenantId} />}
    </AdminShell>
  );
}
