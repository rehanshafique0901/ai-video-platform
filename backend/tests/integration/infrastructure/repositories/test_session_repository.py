"""Integration tests for ``SessionRepository`` — real DB, SAVEPOINT isolation.

α2a covers ``add`` only; α2b extends the suite with lookup / revoke /
family-listing tests when those methods are added.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.identity.session import Session as SessionEntity
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.repositories.session_repository import SessionRepository


async def _seed_user(session: AsyncSession) -> tuple:
    tenant_id = uuid4()
    user_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="Sess Test", slug=f"sess-{tenant_id}")
    )
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"sess-{user_id}@example.com",
            display_name="Sess Test",
        )
    )
    return tenant_id, user_id


@pytest.mark.integration
async def test_add_persists_session_row(session: AsyncSession) -> None:
    _, user_id = await _seed_user(session)
    repo = SessionRepository(session)

    now = datetime.now(UTC)
    entity = SessionEntity(
        id=uuid4(),
        user_id=user_id,
        family_id=uuid4(),
        token_hash="a" * 64,
        ip="127.0.0.1",
        user_agent="pytest",
        issued_at=now,
        last_used_at=now,
        expires_at=now + timedelta(days=30),
        revoked_at=None,
    )
    persisted = await repo.add(entity)

    assert persisted.id == entity.id
    assert persisted.family_id == entity.family_id
    assert persisted.token_hash == entity.token_hash
    assert persisted.revoked_at is None
