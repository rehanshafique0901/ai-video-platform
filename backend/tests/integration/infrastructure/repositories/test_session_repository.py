"""Integration tests for ``SessionRepository`` — real DB, SAVEPOINT isolation.

α2a shipped ``add``. α2b adds coverage for ``get_by_hash``, ``revoke``
(CAS semantics), and ``list_family``. These four tests must pass
against a real Postgres BEFORE the ``RefreshSession`` use case is
written — if the repository contract is wrong, we want to know at the
persistence layer, not while debugging orchestration.
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


def _make_entity(user_id, *, family_id=None, token_hash=None, revoked_at=None) -> SessionEntity:
    now = datetime.now(UTC)
    return SessionEntity(
        id=uuid4(),
        user_id=user_id,
        family_id=family_id or uuid4(),
        token_hash=token_hash or ("a" * 63 + str(uuid4())[:1]),
        ip="127.0.0.1",
        user_agent="pytest",
        issued_at=now,
        last_used_at=now,
        expires_at=now + timedelta(days=30),
        revoked_at=revoked_at,
    )


@pytest.mark.integration
async def test_add_persists_session_row(session: AsyncSession) -> None:
    _, user_id = await _seed_user(session)
    repo = SessionRepository(session)

    entity = _make_entity(user_id)
    persisted = await repo.add(entity)

    assert persisted.id == entity.id
    assert persisted.family_id == entity.family_id
    assert persisted.token_hash == entity.token_hash
    assert persisted.revoked_at is None


@pytest.mark.integration
async def test_get_by_hash_returns_row_for_known_hash_and_none_for_unknown(
    session: AsyncSession,
) -> None:
    _, user_id = await _seed_user(session)
    repo = SessionRepository(session)

    entity = _make_entity(user_id, token_hash="deadbeef" * 8)
    await repo.add(entity)

    hit = await repo.get_by_hash("deadbeef" * 8)
    assert hit is not None
    assert hit.id == entity.id

    miss = await repo.get_by_hash("cafebabe" * 8)
    assert miss is None


@pytest.mark.integration
async def test_get_by_hash_returns_revoked_rows_too(session: AsyncSession) -> None:
    """Reuse detection requires the repository NOT to filter revoked rows.

    Filtering would collapse "no such token" and "token was revoked" into
    a single ``None`` return, and ``RefreshSession`` could no longer
    distinguish an unknown-token 401 from a compromise-signal 401.
    """
    _, user_id = await _seed_user(session)
    repo = SessionRepository(session)

    revoked = _make_entity(user_id, token_hash="c" * 64, revoked_at=datetime.now(UTC))
    await repo.add(revoked)

    hit = await repo.get_by_hash("c" * 64)
    assert hit is not None
    assert hit.revoked_at is not None


@pytest.mark.integration
async def test_revoke_is_compare_and_swap_idempotent(session: AsyncSession) -> None:
    """First revoke returns True; second revoke of same row returns False.

    Second-call False means the ``revoked_at`` timestamp is preserved
    (the original logout moment stays authoritative for audit — the
    port contract explicitly promises this).
    """
    _, user_id = await _seed_user(session)
    repo = SessionRepository(session)

    entity = _make_entity(user_id, token_hash="1" * 64)
    await repo.add(entity)

    t1 = datetime.now(UTC)
    first = await repo.revoke(entity.id, at=t1)
    assert first is True

    row_after_first = await repo.get_by_hash("1" * 64)
    assert row_after_first is not None and row_after_first.revoked_at is not None
    original_ts = row_after_first.revoked_at

    t2 = t1 + timedelta(hours=1)
    second = await repo.revoke(entity.id, at=t2)
    assert second is False

    row_after_second = await repo.get_by_hash("1" * 64)
    assert row_after_second is not None
    assert row_after_second.revoked_at == original_ts  # preserved


@pytest.mark.integration
async def test_revoke_on_unknown_session_id_returns_false(session: AsyncSession) -> None:
    _, user_id = await _seed_user(session)
    repo = SessionRepository(session)

    result = await repo.revoke(uuid4(), at=datetime.now(UTC))
    assert result is False


@pytest.mark.integration
async def test_list_family_returns_all_rows_regardless_of_revocation(
    session: AsyncSession,
) -> None:
    _, user_id = await _seed_user(session)
    repo = SessionRepository(session)

    family_id = uuid4()
    other_family = uuid4()

    s1 = _make_entity(
        user_id, family_id=family_id, token_hash="f1" + "a" * 62, revoked_at=datetime.now(UTC)
    )
    s2 = _make_entity(user_id, family_id=family_id, token_hash="f1" + "b" * 62)
    s3 = _make_entity(user_id, family_id=other_family, token_hash="f2" + "c" * 62)
    for e in (s1, s2, s3):
        await repo.add(e)

    family_rows = await repo.list_family(family_id)
    ids = {row.id for row in family_rows}
    assert ids == {s1.id, s2.id}  # both members; s3 excluded (different family)

    empty = await repo.list_family(uuid4())
    assert empty == []
