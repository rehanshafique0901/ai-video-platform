"""Integration tests for ``UserRepository``.

Runs against the live database; each test is wrapped in a SAVEPOINT
that rolls back on teardown, so no rows persist. α2a extends the
suite with the new port surface (``get_by_email`` / ``get_by_id`` /
``add`` / ``update_last_login``); α1 smoke tests
(``count`` / ``exists_by_id``) are kept per the pre-flight review.
α4 adds the version-fenced CAS (``update_profile``) that underpins
``PATCH /users/me`` (R1–R3, see pre-flight §5.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update
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


# ---- α4 additions — update_profile version-fenced CAS (R1–R3) ---------
#
# These exercise the compare-and-swap that underpins PATCH /users/me
# (pre-flight §5.2), directly through the ORM ``session`` fixture for a
# focused failure signal. The full-stack path is covered by
# ``test_users_me.py`` H15–H24. Trigger interactions:
#   * touch_updated_at fires on real UPDATEs but uses now() =
#     transaction_timestamp(), constant within the SAVEPOINT fixture's
#     transaction — so R1 asserts ``updated_at`` presence, not strict
#     ordering (same weakening as the HTTP-layer H15).
#   * bump_version fires only ``IF NEW.version = OLD.version``; the CAS
#     sets ``NEW.version = OLD.version + 1`` explicitly, so the trigger
#     skips (no double bump). R1's ``version == 2`` assertion also
#     proves this.


async def _seed_user_for_update(
    session: AsyncSession, display_name: str = "Original"
) -> UserEntity:
    """Persist a fresh user via ``repo.add`` and return the DB-populated entity.

    Reuses :func:`_seed_tenant` for the parent tenant. The returned
    entity carries the DB-generated ``created_at`` / ``updated_at`` /
    ``version`` (1) so the CAS fence has a real starting version.
    """
    tenant_id = await _seed_tenant(session)
    repo = UserRepository(session)
    now = datetime.now(UTC)
    entity = UserEntity(
        id=uuid4(),
        tenant_id=tenant_id,
        email=f"upd-{uuid4()}@example.com",
        password_hash="$argon2id$fake",
        display_name=display_name,
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=1,
    )
    return await repo.add(entity)


@pytest.mark.integration
async def test_r1_update_profile_happy_path_bumps_version(session: AsyncSession) -> None:
    """R1: real row shows the new ``display_name`` and ``version+1``
    after a successful CAS; a fresh ``get_by_id`` confirms the mutation
    is persisted (not just returned by ``RETURNING``); ``version == 2``
    proves the ``bump_version`` trigger did not double-bump."""
    seeded = await _seed_user_for_update(session, display_name="Original")
    assert seeded.version == 1

    repo = UserRepository(session)
    updated = await repo.update_profile(
        user_id=seeded.id,
        expected_version=1,
        display_name="Updated",
    )

    assert updated is not None
    assert updated.id == seeded.id
    assert updated.display_name == "Updated"
    assert updated.version == 2, (
        "expected version=2 after a single CAS; version>2 would mean the "
        "bump_version trigger double-bumped after the application-side +1"
    )
    assert updated.updated_at is not None  # presence, not strict ordering — see header

    fetched = await repo.get_by_id(seeded.id)
    assert fetched is not None
    assert fetched.display_name == "Updated"
    assert fetched.version == 2


@pytest.mark.integration
async def test_r2_update_profile_version_mismatch_returns_none(session: AsyncSession) -> None:
    """R2: a stale ``expected_version`` yields ``None`` (the caller then
    raises ``VersionConflictError`` → 412). The row is exactly the
    pre-call state — no partial write, no silent bump."""
    seeded = await _seed_user_for_update(session, display_name="Untouched")

    repo = UserRepository(session)
    result = await repo.update_profile(
        user_id=seeded.id,
        expected_version=99,  # real version is 1
        display_name="Would-Be Change",
    )
    assert result is None

    fetched = await repo.get_by_id(seeded.id)
    assert fetched is not None
    assert fetched.display_name == "Untouched"
    assert fetched.version == 1


@pytest.mark.integration
async def test_r3_update_profile_on_soft_deleted_row_returns_none(session: AsyncSession) -> None:
    """R3: a soft-deleted row (``deleted_at IS NOT NULL``) is not
    updateable even with the correct version — the ``deleted_at IS NULL``
    filter on the fetch + UPDATE guarantees this. Upstream this collapses
    to 412 ``VERSION_CONFLICT``, indistinguishable from a stale-version
    race (A10, anti-enumeration)."""
    seeded = await _seed_user_for_update(session, display_name="AboutToBeDeleted")

    # Soft-delete directly via ORM — UserRepository has no delete method
    # in α4; this keeps the test focused on update_profile's behaviour
    # against an already-soft-deleted row.
    await session.execute(
        update(User).where(User.id == seeded.id).values(deleted_at=datetime.now(UTC))
    )
    await session.flush()

    repo = UserRepository(session)
    result = await repo.update_profile(
        user_id=seeded.id,
        expected_version=1,
        display_name="Change Attempt",
    )
    assert result is None
