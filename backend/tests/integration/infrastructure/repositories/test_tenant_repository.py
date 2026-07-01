"""Integration tests for ``TenantRepository`` — real DB, SAVEPOINT isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.identity.tenant import Tenant
from app.infrastructure.repositories.tenant_repository import TenantRepository


def _make_tenant(slug_suffix: str) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=uuid4(),
        name="Integration Test Workspace",
        slug=f"integration-{slug_suffix}",
        plan_tier="free",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.integration
async def test_add_persists_and_roundtrips(session: AsyncSession) -> None:
    repo = TenantRepository(session)
    tenant = _make_tenant(str(uuid4()))

    persisted = await repo.add(tenant)

    assert persisted.id == tenant.id
    assert persisted.slug == tenant.slug
    assert persisted.plan_tier == "free"
    # DB defaults populated the audit columns.
    assert persisted.created_at is not None
    assert persisted.updated_at is not None


@pytest.mark.integration
async def test_add_raises_conflict_on_slug_collision(session: AsyncSession) -> None:
    import dataclasses

    repo = TenantRepository(session)
    slug = f"collide-{uuid4()}"
    first = dataclasses.replace(_make_tenant("a"), slug=slug)
    second = dataclasses.replace(_make_tenant("b"), slug=slug)

    await repo.add(first)
    with pytest.raises(ConflictError):
        await repo.add(second)


@pytest.mark.integration
async def test_get_by_id_returns_persisted_tenant(session: AsyncSession) -> None:
    repo = TenantRepository(session)
    tenant = _make_tenant(str(uuid4()))
    await repo.add(tenant)

    fetched = await repo.get_by_id(tenant.id)

    assert fetched is not None
    assert fetched.id == tenant.id


@pytest.mark.integration
async def test_get_by_id_returns_none_for_unknown(session: AsyncSession) -> None:
    repo = TenantRepository(session)
    assert await repo.get_by_id(uuid4()) is None


@pytest.mark.integration
async def test_exists_by_slug(session: AsyncSession) -> None:
    repo = TenantRepository(session)
    tenant = _make_tenant(str(uuid4()))
    await repo.add(tenant)

    assert await repo.exists_by_slug(tenant.slug) is True
    assert await repo.exists_by_slug(f"nonexistent-{uuid4()}") is False
