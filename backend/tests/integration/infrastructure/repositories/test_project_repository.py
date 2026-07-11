"""Integration tests for ``ProjectRepository`` (Slice α5a).

Runs against the live database; each test is wrapped in a SAVEPOINT
that rolls back on teardown, so no rows persist. Covers the three α5a
methods — ``add`` (create), ``get_owned`` (scoped single read), and
``list_owned`` (owner-scoped, keyset-paginated, newest-first) — plus
the live-row uniqueness constraint and soft-delete / cross-owner
invisibility.

Note on ``created_at`` for the ordering tests (R6/R7): the DB
``server_default`` is ``now()`` = ``transaction_timestamp()``, which is
**constant** within the SAVEPOINT fixture's single transaction — so
rows inserted via ``repo.add`` would all share one timestamp. To
exercise the ``created_at DESC`` primary sort honestly, the ordering
tests insert rows via a direct core ``insert`` with explicit, staggered
``created_at`` values. The ``id DESC`` tie-break (α5a D14) is what makes
pagination stable when timestamps DO collide, and is exercised
implicitly by every page boundary.

Coverage map (α5a pre-flight §8):

* R1 — ``add`` persists and returns a populated entity (``version == 1``,
  DB timestamps, ``settings`` / ``language`` defaults honoured).
* R2 — ``add`` raises ``ConflictError`` on a duplicate live
  ``(tenant, owner, name)``.
* R3 — the same name IS allowed for a different owner (constraint is
  per-owner, not tenant-wide).
* R4 — ``get_owned`` returns the owner's row.
* R5 — ``get_owned`` returns ``None`` for a cross-owner row and for a
  soft-deleted row (scoping + soft-delete invisibility).
* R6 — ``list_owned`` returns the owner's rows newest-first and excludes
  foreign-owner rows.
* R7 — ``list_owned`` keyset pagination walks every row exactly once,
  in order, across pages.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.projects.project import Project as ProjectEntity
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.repositories.project_repository import ProjectRepository


async def _seed_owner(session: AsyncSession) -> tuple[UUID, UUID]:
    """Insert a tenant + user and return ``(tenant_id, owner_user_id)``."""
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="PR Test", slug=f"pr-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"pr-{user_id}@example.com",
            display_name="PR Owner",
        )
    )
    return tenant_id, user_id


def _entity(*, tenant_id: UUID, owner_user_id: UUID, name: str) -> ProjectEntity:
    now = datetime.now(UTC)
    return ProjectEntity(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        folder_id=None,
        current_version_id=None,
        name=name,
        description=None,
        aspect_ratio="horizontal",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=now,
        updated_at=now,
        version=1,
    )


async def _insert_project(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    name: str,
    created_at: datetime,
) -> UUID:
    """Insert a project with an explicit ``created_at`` (bypasses repo.add).

    Used only by the ordering/pagination tests so the ``created_at DESC``
    sort has distinct timestamps to order by (the fixture's
    transaction-constant ``now()`` cannot provide that).
    """
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=name,
            aspect_ratio="horizontal",
            created_at=created_at,
        )
    )
    return project_id


# ---- R1 — add persists + populated entity ----------------------------


@pytest.mark.integration
async def test_r1_add_persists_and_returns_populated_entity(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)

    persisted = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="First"))

    assert persisted.name == "First"
    assert persisted.version == 1
    assert persisted.created_at is not None
    assert persisted.updated_at is not None
    assert persisted.language == "en"  # server default honoured
    assert persisted.settings == {}  # server default honoured
    assert persisted.current_version_id is None
    assert persisted.duration_seconds is None


# ---- R2/R3 — uniqueness scope -----------------------------------------


@pytest.mark.integration
async def test_r2_add_duplicate_name_same_owner_raises_conflict(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Dup"))

    with pytest.raises(ConflictError):
        await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Dup"))


@pytest.mark.integration
async def test_r3_same_name_allowed_for_different_owner(session: AsyncSession) -> None:
    tenant_id, owner_a = await _seed_owner(session)
    # A second owner in the same tenant.
    owner_b = uuid4()
    await session.execute(
        insert(User).values(
            id=owner_b,
            tenant_id=tenant_id,
            email=f"pr-{owner_b}@example.com",
            display_name="Owner B",
        )
    )
    repo = ProjectRepository(session)

    await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_a, name="Shared"))
    # Must NOT raise — uniqueness is per (tenant, owner, name).
    other = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_b, name="Shared"))
    assert other.name == "Shared"


# ---- R4/R5 — get_owned scoping ----------------------------------------


@pytest.mark.integration
async def test_r4_get_owned_returns_owned_row(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    created = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Mine"))

    fetched = await repo.get_owned(
        project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id
    )
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.integration
async def test_r5_get_owned_hides_cross_owner_and_soft_deleted(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    created = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Scoped"))

    # Cross-owner: same row id, different owner → None.
    assert (
        await repo.get_owned(project_id=created.id, tenant_id=tenant_id, owner_user_id=uuid4())
    ) is None

    # Soft-delete then fetch as the real owner → None.
    await session.execute(
        update(ProjectRow).where(ProjectRow.id == created.id).values(deleted_at=datetime.now(UTC))
    )
    await session.flush()
    assert (
        await repo.get_owned(project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id)
    ) is None


# ---- R6 — list_owned ordering + scoping -------------------------------


@pytest.mark.integration
async def test_r6_list_owned_newest_first_and_owner_scoped(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    other_owner = uuid4()
    await session.execute(
        insert(User).values(
            id=other_owner,
            tenant_id=tenant_id,
            email=f"pr-{other_owner}@example.com",
            display_name="Other",
        )
    )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Owner's rows at t+0, t+1, t+2 (newest last-inserted).
    ids = [
        await _insert_project(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            name=f"P{i}",
            created_at=base + timedelta(seconds=i),
        )
        for i in range(3)
    ]
    # A foreign-owner row that must be excluded.
    await _insert_project(
        session,
        tenant_id=tenant_id,
        owner_user_id=other_owner,
        name="Foreign",
        created_at=base + timedelta(seconds=5),
    )
    await session.flush()

    repo = ProjectRepository(session)
    rows = await repo.list_owned(tenant_id=tenant_id, owner_user_id=owner_id, limit=20)

    # Newest first → reversed insertion order; foreign row excluded.
    assert [r.id for r in rows] == list(reversed(ids))


# ---- R7 — list_owned keyset pagination --------------------------------


@pytest.mark.integration
async def test_r7_list_owned_keyset_pagination_covers_all_rows(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    base = datetime(2026, 2, 1, tzinfo=UTC)
    ids = [
        await _insert_project(
            session,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            name=f"K{i}",
            created_at=base + timedelta(seconds=i),
        )
        for i in range(5)
    ]
    await session.flush()
    expected = list(reversed(ids))  # newest-first

    repo = ProjectRepository(session)
    page1 = await repo.list_owned(tenant_id=tenant_id, owner_user_id=owner_id, limit=2)
    assert [r.id for r in page1] == expected[:2]

    last = page1[-1]
    page2 = await repo.list_owned(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        limit=2,
        after=(last.created_at, last.id),
    )
    assert [r.id for r in page2] == expected[2:4]

    last2 = page2[-1]
    page3 = await repo.list_owned(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        limit=2,
        after=(last2.created_at, last2.id),
    )
    assert [r.id for r in page3] == expected[4:]
    assert len(page3) == 1  # last page, partial
