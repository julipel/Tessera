import type { AdminMe } from "@/contracts";

/** Может ли пользователь менять тенант: роль editor или суперадмин (ADR-0036). */
export function canEdit(me: AdminMe, tenantId: string): boolean {
  return me.is_superadmin || me.memberships.some((m) => m.tenant_id === tenantId && m.role === "editor");
}

/** Название тенанта из ролей пользователя (у суперадмина без роли — нет). */
export function tenantName(me: AdminMe, tenantId: string): string | null {
  return me.memberships.find((m) => m.tenant_id === tenantId)?.tenant_name ?? null;
}
