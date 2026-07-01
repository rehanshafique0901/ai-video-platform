"""Integration tests for ``UserRepository``.

Runs against the live database; each test is wrapped in a SAVEPOINT
that rolls back on teardown, so no rows persist. α2a extends the
suite with the new port surface (``get_by_email`` / ``get_by_id`` /
``add`` / ``update_last_login``); α1 smoke tests
(``count`` / ``exists_by_id``) are kept per the pre-flight review.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.identity.user import User as UserEntity
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.repositories.user_repository import UserRepository


async def _seed_tenant(session: AsyncSession):  # type: ignore[no-untyped-def]
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="UR Test", slug=f"ur-{tenant_id}")
    )
    return tenant_id


@pytest.mark.integration
async def test_count_returns_non_negative_int(session: AsyncSession) -> None:
    """``count`` must always return an int ≥ 0, regardless of pre-existing rows."""
    repo = UserRepository(session)
    n = await repo.count()
    assert isinstance(n, int)
    assert n >= 0


@pytest.mark.integration
async def test_count_increments_when_user_inserted(session: AsyncSession) -> None:
    repo = UserRepository(session)
    before = await repo.count()

    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(
            id=tenant_id,
            name="α1 test tenant",
            slug=f"alpha1-{tenant_id}",
        )
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"alpha1-{user_id}@example.com",
            display_name="α1 test user",
        )
    )

    after = await repo.count()
    assert after == before + 1


@pytest.mark.integration
async def test_exists_by_id_returns_true_for_existing(session: AsyncSession) -> None:
    repo = UserRepository(session)
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="α1 test tenant", slug=f"alpha1-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"alpha1-{user_id}@example.com",
            display_name="α1 test user",
        )
    )

    assert await repo.exists_by_id(user_id) is True


@pytest.mark.integration
async def test_exists_by_id_returns_false_for_unknown(session: AsyncSession) -> None:
    repo = UserRepository(session)
    assert await repo.exists_by_id(uuid4()) is False


# ---- α2a additions ---------------------------------------------------


@pytest.mark.integration
async def test_add_persists_user_and_returns_populated_entity(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    repo = UserRepository(session)
    now = datetime.now(UTC)
    entity = UserEntity(
        id=uuid4(),
        tenant_id=tenant_id,
        email=f"add-{uuid4()}@example.com",
        password_hash="$argon2id$fake-digest",
        display_name="Added User",
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=1,
    )
    persisted = await repo.add(entity)
    assert persisted.id == entity.id
    assert persisted.email == entity.email
    assert persisted.created_at is not None
    assert persisted.version == 1


@pytest.mark.integration
async def test_add_raises_conflict_on_duplicate_email_within_tenant(
    session: AsyncSession,
) -> None:
    tenant_id = await _seed_tenant(session)
    repo = UserRepository(session)
    email = f"dup-{uuid4()}@example.com"
    now = datetime.now(UTC)
    base = UserEntity(
        id=uuid4(),
        tenant_id=tenant_id,
        email=email,
        password_hash="$argon2id$fake",
        display_name="First",
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=1,
    )
    import dataclasses

    second = dataclasses.replace(base, id=uuid4(), display_name="Second")

    await repo.add(base)
    with pytest.raises(ConflictError):
        await repo.add(second)


@pytest.mark.integration
async def test_get_by_email_returns_persisted_user(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    repo = UserRepository(session)
    email = f"lookup-{uuid4()}@example.com"
    now = datetime.now(UTC)
    entity = UserEntity(
        id=uuid4(),
        tenant_id=tenant_id,
        email=email,
        password_hash="$argon2id$fake",
        display_name="Lookup",
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=1,
    )
    await repo.add(entity)

    fetched = await repo.get_by_email(email)
    assert fetched is not None
    assert fetched.id == entity.id


@pytest.mark.integration
async def test_get_by_email_returns_none_for_unknown(session: AsyncSession) -> None:
    repo = UserRepository(session)
    assert await repo.get_by_email(f"ghost-{uuid4()}@example.com") is None


@pytest.mark.integration
async def test_get_by_id_returns_persisted_user(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    repo = UserRepository(session)
    now = datetime.now(UTC)
    entity = UserEntity(
        id=uuid4(),
        tenant_id=tenant_id,
        email=f"byid-{uuid4()}@example.com",
        password_hash="$argon2id$fake",
        display_name="ById",
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=1,
    )
    await repo.add(entity)

    fetched = await repo.get_by_id(entity.id)
    assert fetched is not None
    assert fetched.email == entity.email


@pytest.mark.integration
async def test_update_last_login_sets_column(session: AsyncSession) -> None:
    tenant_id = await _seed_tenant(session)
    repo = UserRepository(session)
    now = datetime.now(UTC)
    entity = UserEntity(
        id=uuid4(),
        tenant_id=tenant_id,
        email=f"ll-{uuid4()}@example.com",
        password_hash="$argon2id$fake",
        display_name="LastLogin",
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=1,
    )
    await repo.add(entity)

    await repo.update_last_login(entity.id, now)
    persisted_last = (
        await session.execute(select(User.last_login_at).where(User.id == entity.id))
    ).scalar_one()
    assert persisted_last is not None
