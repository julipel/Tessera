"""Пользователи админки и роли в тенантах (P8-01a, ADR-0036): пароли, хранение, CLI."""

import base64
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import parse_grants, read_password
from app.modules.access.domain.passwords import (
    check_password_strength,
    hash_password,
    verify_password,
)
from app.modules.access.public import (
    AdminMembershipRecord,
    AdminMembershipRepository,
    AdminRole,
    AdminUserRecord,
    InvalidEmailError,
    PasswordRequiredError,
    SqlAdminUserDirectory,
    TenantMembership,
    WeakPasswordError,
    normalize_email,
    upsert_admin_user,
)
from app.modules.shared.public import NotFoundError, TenantId, TenantMismatchError
from app.modules.tenants.public import SqlTenantDirectory, TenantRecord

PASSWORD = "correct horse battery"


# --- Пароли и email ---


def test_password_hash_verifies_only_same_password() -> None:
    encoded = hash_password(PASSWORD)

    assert encoded.startswith("scrypt$")
    assert PASSWORD not in encoded
    assert verify_password(PASSWORD, encoded)
    assert not verify_password(PASSWORD + "!", encoded)


def test_password_hash_is_salted() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


@pytest.mark.parametrize("encoded", ["", "plain", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$AA==$AA=="])
def test_malformed_hash_does_not_verify(encoded: str) -> None:
    assert not verify_password(PASSWORD, encoded)


def test_hash_keeps_its_parameters() -> None:
    # Хэш со старыми (слабее) параметрами проверяется со своими — их можно менять без миграции.
    salt = b"0123456789abcdef"
    key = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=2**10, r=8, p=1, dklen=32)
    encoded = f"scrypt$1024$8$1${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"

    assert verify_password(PASSWORD, encoded)


def test_short_password_is_rejected() -> None:
    with pytest.raises(WeakPasswordError):
        check_password_strength("short")


def test_email_is_normalized() -> None:
    assert normalize_email("  Admin@Example.COM ") == "admin@example.com"


@pytest.mark.parametrize("email", ["", "admin", "a@b", "a b@c.d", "a@@b.c"])
def test_invalid_email_is_rejected(email: str) -> None:
    with pytest.raises(InvalidEmailError):
        normalize_email(email)


def test_editor_includes_viewer() -> None:
    assert AdminRole.EDITOR.allows(AdminRole.VIEWER)
    assert AdminRole.EDITOR.allows(AdminRole.EDITOR)
    assert AdminRole.VIEWER.allows(AdminRole.VIEWER)
    assert not AdminRole.VIEWER.allows(AdminRole.EDITOR)


# --- Хранение ---


@pytest.fixture
async def tenants(db_session: AsyncSession) -> tuple[TenantId, TenantId]:
    directory = SqlTenantDirectory(db_session)
    a = await directory.create("tenant-a", "A")
    b = await directory.create("tenant-b", "B")
    return a.id, b.id


@pytest.fixture
def users(db_session: AsyncSession) -> SqlAdminUserDirectory:
    return SqlAdminUserDirectory(db_session)


@pytest.fixture
def memberships(db_session: AsyncSession) -> AdminMembershipRepository:
    return AdminMembershipRepository(db_session)


async def test_email_is_unique(users: SqlAdminUserDirectory) -> None:
    await users.create("a@example.com", "h", is_superadmin=False)

    with pytest.raises(IntegrityError):
        await users.create("a@example.com", "h", is_superadmin=False)


async def test_membership_is_scoped_by_tenant(
    users: SqlAdminUserDirectory,
    memberships: AdminMembershipRepository,
    tenants: tuple[TenantId, TenantId],
) -> None:
    a, b = tenants
    user = await users.create("a@example.com", "h", is_superadmin=False)
    await memberships.set_role(a, user.id, AdminRole.EDITOR)

    assert await memberships.get_role(a, user.id) is AdminRole.EDITOR
    assert await memberships.get_role(b, user.id) is None
    assert await memberships.revoke(b, user.id) is False
    assert await memberships.get_role(a, user.id) is AdminRole.EDITOR
    with pytest.raises(TenantMismatchError):
        await memberships.add(
            a, AdminMembershipRecord(tenant_id=b, user_id=user.id, role=AdminRole.VIEWER)
        )


async def test_one_role_per_tenant(
    db_session: AsyncSession,
    users: SqlAdminUserDirectory,
    memberships: AdminMembershipRepository,
    tenants: tuple[TenantId, TenantId],
) -> None:
    a, b = tenants
    user = await users.create("a@example.com", "h", is_superadmin=False)
    await memberships.set_role(a, user.id, AdminRole.VIEWER)
    await memberships.set_role(a, user.id, AdminRole.EDITOR)
    await memberships.set_role(b, user.id, AdminRole.VIEWER)

    assert set(await users.memberships(user.id)) == {
        TenantMembership(a, AdminRole.EDITOR),
        TenantMembership(b, AdminRole.VIEWER),
    }
    rows = (await db_session.execute(select(AdminMembershipRecord))).scalars().all()
    assert len(rows) == 2


async def test_unknown_role_is_rejected_by_db(
    db_session: AsyncSession, users: SqlAdminUserDirectory, tenants: tuple[TenantId, TenantId]
) -> None:
    user = await users.create("a@example.com", "h", is_superadmin=False)
    db_session.add(AdminMembershipRecord(tenant_id=tenants[0], user_id=user.id, role="owner"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_tenant_deletion_removes_its_memberships(
    db_session: AsyncSession,
    users: SqlAdminUserDirectory,
    memberships: AdminMembershipRepository,
    tenants: tuple[TenantId, TenantId],
) -> None:
    a, b = tenants
    user = await users.create("a@example.com", "h", is_superadmin=False)
    await memberships.set_role(a, user.id, AdminRole.VIEWER)
    await memberships.set_role(b, user.id, AdminRole.VIEWER)

    await db_session.execute(delete(TenantRecord).where(TenantRecord.id == a))

    assert await users.memberships(user.id) == [TenantMembership(b, AdminRole.VIEWER)]


# --- Создание и изменение пользователя ---


async def test_new_user_needs_password(
    users: SqlAdminUserDirectory, memberships: AdminMembershipRepository
) -> None:
    with pytest.raises(PasswordRequiredError):
        await upsert_admin_user("a@example.com", users=users, memberships=memberships)
    assert await users.get_by_email("a@example.com") is None


async def test_upsert_creates_user_with_roles(
    db_session: AsyncSession,
    users: SqlAdminUserDirectory,
    memberships: AdminMembershipRepository,
    tenants: tuple[TenantId, TenantId],
) -> None:
    a, _ = tenants

    result = await upsert_admin_user(
        " Ops@Example.com ",
        users=users,
        memberships=memberships,
        password=PASSWORD,
        grants={a: AdminRole.VIEWER},
    )

    assert result.created
    assert (result.user.email, result.user.is_superadmin, result.user.is_active) == (
        "ops@example.com",
        False,
        True,
    )
    assert result.memberships == [TenantMembership(a, AdminRole.VIEWER)]
    record = await db_session.get(AdminUserRecord, result.user.id)
    assert record is not None
    assert verify_password(PASSWORD, record.password_hash)


async def test_upsert_changes_only_given_fields(
    db_session: AsyncSession,
    users: SqlAdminUserDirectory,
    memberships: AdminMembershipRepository,
    tenants: tuple[TenantId, TenantId],
) -> None:
    a, b = tenants
    first = await upsert_admin_user(
        "ops@example.com",
        users=users,
        memberships=memberships,
        password=PASSWORD,
        is_superadmin=True,
        grants={a: AdminRole.VIEWER, b: AdminRole.VIEWER},
    )
    record = await db_session.get(AdminUserRecord, first.user.id)
    assert record is not None
    old_hash = record.password_hash

    result = await upsert_admin_user(
        "OPS@example.com",
        users=users,
        memberships=memberships,
        is_active=False,
        grants={a: AdminRole.EDITOR},
        revokes=[b],
    )

    assert not result.created
    assert result.user.id == first.user.id
    assert (result.user.is_superadmin, result.user.is_active) == (True, False)
    assert result.memberships == [TenantMembership(a, AdminRole.EDITOR)]
    assert record.password_hash == old_hash


async def test_upsert_resets_password(
    db_session: AsyncSession, users: SqlAdminUserDirectory, memberships: AdminMembershipRepository
) -> None:
    created = await upsert_admin_user(
        "ops@example.com", users=users, memberships=memberships, password=PASSWORD
    )

    await upsert_admin_user(
        "ops@example.com", users=users, memberships=memberships, password="another long secret"
    )

    record = await db_session.get(AdminUserRecord, created.user.id)
    assert record is not None
    assert verify_password("another long secret", record.password_hash)
    assert not verify_password(PASSWORD, record.password_hash)


async def test_weak_password_changes_nothing(
    users: SqlAdminUserDirectory, memberships: AdminMembershipRepository
) -> None:
    with pytest.raises(WeakPasswordError):
        await upsert_admin_user(
            "ops@example.com", users=users, memberships=memberships, password="short"
        )
    assert await users.get_by_email("ops@example.com") is None


async def test_unknown_user_update_is_not_found(users: SqlAdminUserDirectory) -> None:
    with pytest.raises(NotFoundError):
        await users.update(uuid4(), is_active=False)


# --- CLI ---


def test_parse_grants() -> None:
    assert parse_grants([" a = viewer ", "b=editor", "a=editor"]) == {
        "a": AdminRole.EDITOR,
        "b": AdminRole.EDITOR,
    }


@pytest.mark.parametrize("value", ["a", "a=", "=viewer", "a=owner"])
def test_parse_grants_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError, match="slug=role"):
        parse_grants([value])


def test_password_from_env_skips_prompt() -> None:
    def no_prompt(_: str) -> str:
        raise AssertionError("prompt")

    assert read_password({"ADMIN_PASSWORD": PASSWORD}, no_prompt) == PASSWORD


def test_prompted_password_must_match() -> None:
    answers = iter([PASSWORD, PASSWORD + "x"])

    with pytest.raises(ValueError, match="не совпадают"):
        read_password({}, lambda _: next(answers))

    answers = iter([PASSWORD, PASSWORD])
    assert read_password({}, lambda _: next(answers)) == PASSWORD
