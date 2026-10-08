import Link from "next/link";
import type { AdminMe } from "@/contracts";

const ROLE_LABELS = { viewer: "просмотр", editor: "редактор" } as const;

/** Тенанты, где у пользователя есть роль. Источники видны любой роли, конфиг — editor. */
export function TenantList({ me }: { me: AdminMe }) {
  if (me.memberships.length === 0) {
    return (
      <p className="text-chat-muted">
        {me.is_superadmin
          ? "Нет ролей в тенантах. Список всех тенантов для суперадмина пока не реализован — выдайте себе роль: make admin-user."
          : "У вас нет доступа ни к одному тенанту."}
      </p>
    );
  }
  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Тенанты</h1>
      <ul className="divide-y divide-chat-border rounded-chat border border-chat-border bg-chat-surface">
        {me.memberships.map((m) => (
          <li key={m.tenant_id} className="flex items-center gap-4 px-4 py-3">
            <div className="flex-1">
              <div className="font-medium">{m.tenant_name}</div>
              <div className="text-sm text-chat-muted">
                {m.tenant_slug} · {ROLE_LABELS[m.role]}
              </div>
            </div>
            <Link
              href={`/admin/tenants/${m.tenant_id}/sources`}
              className="text-sm text-chat-primary hover:underline"
            >
              Источники
            </Link>
            {(m.role === "editor" || me.is_superadmin) && (
              <Link
                href={`/admin/tenants/${m.tenant_id}/config`}
                className="text-sm text-chat-primary hover:underline"
              >
                Конфиг агента
              </Link>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
