"""Создание и изменение пользователя админки — для CLI (`make admin-user`), позже — для API."""

import asyncio
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from app.modules.access.domain.entities import AdminRole, AdminUser, TenantMembership
from app.modules.access.domain.errors import PasswordRequiredError
from app.modules.access.domain.passwords import (
    check_password_strength,
    hash_password,
    normalize_email,
)
from app.modules.access.domain.ports import AdminMembershipStore, AdminUserDirectory
from app.modules.shared.kernel import TenantId


@dataclass(frozen=True, slots=True)
class AdminUserResult:
    user: AdminUser
    created: bool
    memberships: Sequence[TenantMembership]


async def upsert_admin_user(
    email: str,
    *,
    users: AdminUserDirectory,
    memberships: AdminMembershipStore,
    password: str | None = None,
    is_superadmin: bool | None = None,
    is_active: bool | None = None,
    grants: Mapping[TenantId, AdminRole] | None = None,
    revokes: Collection[TenantId] = (),
) -> AdminUserResult:
    """Создать пользователя или изменить переданное: пароль, флаги, роли в тенантах.

    Новому пользователю нужен пароль; `None` в остальных параметрах — не менять. `grants`
    задаёт роль в тенанте (одна на тенант, прежняя заменяется), `revokes` снимает её.
    """
    email = normalize_email(email)
    password_hash: str | None = None
    if password is not None:
        check_password_strength(password)
        password_hash = await asyncio.to_thread(hash_password, password)

    user = await users.get_by_email(email)
    created = user is None
    if user is None:
        if password_hash is None:
            raise PasswordRequiredError(f"{email}: новому пользователю нужен пароль")
        user = await users.create(email, password_hash, bool(is_superadmin))
        if is_active is False:
            user = await users.update(user.id, is_active=False)
    elif password_hash is not None or is_superadmin is not None or is_active is not None:
        user = await users.update(
            user.id,
            password_hash=password_hash,
            is_superadmin=is_superadmin,
            is_active=is_active,
        )

    for tenant_id, role in (grants or {}).items():
        await memberships.set_role(tenant_id, user.id, role)
    for tenant_id in revokes:
        await memberships.revoke(tenant_id, user.id)
    return AdminUserResult(user=user, created=created, memberships=await users.memberships(user.id))
