"""Integration tests for ``UserRepository``.

Runs against the live database; each test is wrapped in a SAVEPOINT
that rolls back on teardown, so no rows persist.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.repositories.user_repository import UserRepository


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
