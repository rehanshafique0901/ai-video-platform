"""Integration tests for ``RoleRepository`` — real DB, SAVEPOINT isolation.

Requires the ``roles`` table to be seeded by migration ``0002_seed_system_data``.

**Note on role codes used here.** The seed migration populates the
``roles`` table with WORKSPACE-PERMISSION codes only:
``owner, admin, editor, viewer, billing, support``. It does NOT
insert ``user`` — that name lives on the ``auth_role`` ENUM
(``schema.md`` §0.1, a separate plan-tier concept). Tests below
therefore use ``owner`` (the code that ``RegisterUser`` actually
assigns) and ``editor`` (a second valid code for the multi-role
scenario).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.infrastructure.db.models.identity import Role, RoleUser, Tenant, User
from app.infrastructure.repositories.role_repository import RoleRepository


async def _seed_user(session: AsyncSession):  # type: ignore[no-untyped-def]
    tenant_id = uuid4()
    user_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="Role Test", slug=f"role-{tenant_id}")
    )
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"role-{user_id}@example.com",
            display_name="Role Test",
        )
    )
    return user_id


@pytest.mark.integration
async def test_assign_role_by_code_inserts_join_row(session: AsyncSession) -> None:
    user_id = await _seed_user(session)
    repo = RoleRepository(session)

    await repo.assign_role_by_code(user_id=user_id, role_code="owner")

    role_id = (await session.execute(select(Role.id).where(Role.code == "owner"))).scalar_one()
    hit = (
        await session.execute(
            select(RoleUser).where(RoleUser.user_id == user_id).where(RoleUser.role_id == role_id)
        )
    ).first()
    assert hit is not None


@pytest.mark.integration
async def test_assign_role_by_code_is_idempotent(session: AsyncSession) -> None:
    """Assigning the same (user, role) twice does not raise or duplicate."""
    user_id = await _seed_user(session)
    repo = RoleRepository(session)

    await repo.assign_role_by_code(user_id=user_id, role_code="owner")
    await repo.assign_role_by_code(user_id=user_id, role_code="owner")

    role_id = (await session.execute(select(Role.id).where(Role.code == "owner"))).scalar_one()
    count = len(
        (
            await session.execute(
                select(RoleUser)
                .where(RoleUser.user_id == user_id)
                .where(RoleUser.role_id == role_id)
            )
        ).all()
    )
    assert count == 1


@pytest.mark.integration
async def test_assign_role_by_code_raises_for_unknown_code(
    session: AsyncSession,
) -> None:
    user_id = await _seed_user(session)
    repo = RoleRepository(session)

    with pytest.raises(NotFoundError):
        await repo.assign_role_by_code(user_id=user_id, role_code="not_a_real_role")


@pytest.mark.integration
async def test_assign_role_by_code_supports_owner_and_editor(
    session: AsyncSession,
) -> None:
    """Two distinct seeded codes must both be assignable on the same user."""
    user_id = await _seed_user(session)
    repo = RoleRepository(session)

    await repo.assign_role_by_code(user_id=user_id, role_code="owner")
    await repo.assign_role_by_code(user_id=user_id, role_code="editor")

    rows = (
        await session.execute(select(RoleUser.role_id).where(RoleUser.user_id == user_id))
    ).all()
    assert len(rows) == 2
