"use client";

import { use } from "react";
import { AdminShell } from "@/components/admin/AdminShell";
import { ConversationView } from "@/components/admin/ConversationView";

export default function TenantConversation({
  params,
}: PageProps<"/admin/tenants/[tenantId]/conversations/[conversationId]">) {
  const { tenantId, conversationId } = use(params);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return (
    <AdminShell apiUrl={apiUrl}>
      {(session) => (
        <ConversationView
          key={conversationId}
          session={session}
          tenantId={tenantId}
          conversationId={conversationId}
        />
      )}
    </AdminShell>
  );
}
