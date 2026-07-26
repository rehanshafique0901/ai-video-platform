"""Integration tests for ``SocialAccountRepository`` (α8.6a).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls back on
teardown. Covers upsert/reconnect, owner-scoping (anti-enumeration), listing order,
revocation, and multi-account-per-(user, platform) (R4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.social_account import AccountStatus
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.publishing import SocialAccount as SocialAccountRow
from app.infrastructure.repositories.social_account_repository import SocialAccountRepository

pytestmark = pytest.mark.integration


async def _seed_user(session: AsyncSession) -> tuple[UUID, UUID]:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="SA Test", slug=f"sa-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"sa-{user_id}@example.com",
            display_name="SA Owner",
        )
    )
    await session.flush()
    return tenant_id, user_id


async def test_upsert_inserts_connected_account(session: AsyncSession) -> None:
    tenant_id, user_id = await _seed_user(session)
    repo = SocialAccountRepository(session)

    account = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-1",
        display_name="Channel One",
        scopes=("publish", "upload"),
    )

    assert account.status is AccountStatus.CONNECTED
    assert account.platform == "mock"
    assert account.external_account_id == "chan-1"
    assert account.scopes == ("publish", "upload")
    assert account.connected_at is not None
    assert account.revoked_at is None


async def test_upsert_reconnect_updates_same_row(session: AsyncSession) -> None:
    tenant_id, user_id = await _seed_user(session)
    repo = SocialAccountRepository(session)
    first = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-1",
        display_name="Old Name",
        scopes=("publish",),
    )
    await repo.mark_revoked(tenant_id=tenant_id, user_id=user_id, social_account_id=first.id)

    reconnected = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-1",
        display_name="New Name",
        scopes=("publish", "upload"),
    )

    assert reconnected.id == first.id  # same row, not a duplicate
    assert reconnected.status is AccountStatus.CONNECTED
    assert reconnected.display_name == "New Name"
    assert reconnected.revoked_at is None
    accounts = await repo.list_for_owner(tenant_id=tenant_id, user_id=user_id)
    assert len(accounts) == 1


async def test_multiple_accounts_per_user_platform(session: AsyncSession) -> None:
    tenant_id, user_id = await _seed_user(session)
    repo = SocialAccountRepository(session)
    a = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-1",
        display_name="One",
        scopes=(),
    )
    b = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-2",
        display_name="Two",
        scopes=(),
    )
    assert a.id != b.id
    accounts = await repo.list_for_owner(tenant_id=tenant_id, user_id=user_id)
    assert {acc.external_account_id for acc in accounts} == {"chan-1", "chan-2"}


async def test_get_owned_is_owner_scoped(session: AsyncSession) -> None:
    tenant_id, user_id = await _seed_user(session)
    other_tenant, other_user = await _seed_user(session)
    repo = SocialAccountRepository(session)
    account = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-1",
        display_name="Mine",
        scopes=(),
    )

    assert (
        await repo.get_owned(tenant_id=tenant_id, user_id=user_id, social_account_id=account.id)
    ) is not None
    # Another principal cannot see it (uniform None → 404 upstream).
    assert (
        await repo.get_owned(
            tenant_id=other_tenant, user_id=other_user, social_account_id=account.id
        )
    ) is None
    assert (
        await repo.get_owned(tenant_id=tenant_id, user_id=user_id, social_account_id=uuid4())
    ) is None


async def test_mark_revoked_is_owner_scoped_and_idempotent(session: AsyncSession) -> None:
    tenant_id, user_id = await _seed_user(session)
    other_tenant, other_user = await _seed_user(session)
    repo = SocialAccountRepository(session)
    account = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-1",
        display_name="Mine",
        scopes=(),
    )

    # Foreign principal cannot revoke it.
    assert (
        await repo.mark_revoked(
            tenant_id=other_tenant, user_id=other_user, social_account_id=account.id
        )
    ) is None

    revoked = await repo.mark_revoked(
        tenant_id=tenant_id, user_id=user_id, social_account_id=account.id
    )
    assert revoked is not None
    assert revoked.status is AccountStatus.REVOKED
    assert revoked.revoked_at is not None

    # Idempotent repeat.
    again = await repo.mark_revoked(
        tenant_id=tenant_id, user_id=user_id, social_account_id=account.id
    )
    assert again is not None
    assert again.status is AccountStatus.REVOKED


async def test_list_is_newest_first_and_owner_scoped(session: AsyncSession) -> None:
    tenant_id, user_id = await _seed_user(session)
    other_tenant, other_user = await _seed_user(session)
    repo = SocialAccountRepository(session)
    first = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-1",
        display_name="First",
        scopes=(),
    )
    second = await repo.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id="chan-2",
        display_name="Second",
        scopes=(),
    )
    await repo.upsert_connected(
        tenant_id=other_tenant,
        user_id=other_user,
        platform="mock",
        external_account_id="chan-x",
        display_name="Foreign",
        scopes=(),
    )
    # ``now()`` is transaction-constant, so all rows seeded in this SAVEPOINT share a
    # ``created_at``; stamp distinct values so the newest-first ordering is deterministic
    # (chan-2 newer than chan-1) rather than resolved by the random-UUID id tiebreak.
    base = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
    await session.execute(
        update(SocialAccountRow).where(SocialAccountRow.id == first.id).values(created_at=base)
    )
    await session.execute(
        update(SocialAccountRow)
        .where(SocialAccountRow.id == second.id)
        .values(created_at=base + timedelta(minutes=1))
    )
    await session.flush()

    mine = await repo.list_for_owner(tenant_id=tenant_id, user_id=user_id)
    assert [a.external_account_id for a in mine] == ["chan-2", "chan-1"]
    assert all(a.user_id == user_id for a in mine)
