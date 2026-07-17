"""Integration tests for ``ProviderSettingsRepository`` (Slice α7.4).

Runs against the live database inside a SAVEPOINT that rolls back on teardown.
Exercises the minimal read path signed off for α7.4 (Q4): a single ``(provider,
key)`` lookup with **tenant-shadows-global** precedence over ``provider_settings``.
Provider names are uuid-suffixed so assertions hold in a shared, non-empty DB.

Coverage:

* S1 — a global row (``tenant_id IS NULL``) is returned; an unset key is ``None``.
* S2 — a tenant-scoped row shadows the global row for the same ``(provider, key)``.
* S3 — with only a global row, a tenant-scoped read falls back to global.
* S4 — reads are isolated per ``(provider, key)``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.configuration import ProviderSetting
from app.infrastructure.db.models.identity import Tenant
from app.infrastructure.repositories.provider_settings_repository import (
    ProviderSettingsRepository,
)


async def _seed_tenant(session: AsyncSession) -> object:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="PS Test", slug=f"ps-{tenant_id}")
    )
    await session.flush()
    return tenant_id


async def _seed_setting(
    session: AsyncSession,
    *,
    provider: str,
    key: str,
    value: dict[str, object],
    tenant_id: object | None = None,
) -> None:
    await session.execute(
        insert(ProviderSetting).values(
            provider=provider,
            key=key,
            value=value,
            tenant_id=tenant_id,
        )
    )
    await session.flush()


@pytest.mark.integration
async def test_s1_global_row_and_missing(session: AsyncSession) -> None:
    repo = ProviderSettingsRepository(session)
    provider = f"prov-{uuid4()}"
    await _seed_setting(session, provider=provider, key="config", value={"enabled": True})

    assert await repo.get_value(provider, "config") == {"enabled": True}
    assert await repo.get_value(provider, "missing") is None


@pytest.mark.integration
async def test_s2_tenant_shadows_global(session: AsyncSession) -> None:
    repo = ProviderSettingsRepository(session)
    provider = f"prov-{uuid4()}"
    tenant_id = await _seed_tenant(session)

    await _seed_setting(session, provider=provider, key="config", value={"scope": "global"})
    await _seed_setting(
        session, provider=provider, key="config", value={"scope": "tenant"}, tenant_id=tenant_id
    )

    assert await repo.get_value(provider, "config", tenant_id=tenant_id) == {"scope": "tenant"}
    # No tenant → the global row.
    assert await repo.get_value(provider, "config") == {"scope": "global"}


@pytest.mark.integration
async def test_s3_tenant_falls_back_to_global(session: AsyncSession) -> None:
    repo = ProviderSettingsRepository(session)
    provider = f"prov-{uuid4()}"
    tenant_id = await _seed_tenant(session)
    await _seed_setting(session, provider=provider, key="config", value={"scope": "global"})

    # Tenant has no override → global is returned.
    assert await repo.get_value(provider, "config", tenant_id=tenant_id) == {"scope": "global"}


@pytest.mark.integration
async def test_s4_isolated_per_key(session: AsyncSession) -> None:
    repo = ProviderSettingsRepository(session)
    provider = f"prov-{uuid4()}"
    await _seed_setting(session, provider=provider, key="a", value={"v": 1})
    await _seed_setting(session, provider=provider, key="b", value={"v": 2})

    assert await repo.get_value(provider, "a") == {"v": 1}
    assert await repo.get_value(provider, "b") == {"v": 2}
