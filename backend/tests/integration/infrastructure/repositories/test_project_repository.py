"""Integration tests for ``ProjectRepository`` (Slices α5a + α5b).

Runs against the live database; each test is wrapped in a SAVEPOINT
that rolls back on teardown, so no rows persist. Covers the α5a read/
create methods — ``add`` (create), ``get_owned`` (scoped single read),
and ``list_owned`` (owner-scoped, keyset-paginated, newest-first) — the
live-row uniqueness constraint and soft-delete / cross-owner
invisibility, and the α5b write methods ``update_owned`` (version-fenced
CAS) and ``soft_delete_owned`` (owner-scoped soft delete).

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
* R8 — ``update_owned`` real change: business field updated; ``version``
  bumps by **exactly 1** (the load-bearing anti-double-bump check — the
  ``tg_projects_biu_version_bump`` trigger is guarded, so hand-setting
  ``version + 1`` must NOT compound to +2).
* R9 — ``update_owned`` version-stale: wrong ``expected_version`` returns
  ``None`` and leaves the row untouched.
* R10 — ``update_owned`` wrong owner/tenant returns ``None``, row
  untouched (scoping).
* R11 — ``update_owned`` rename collision → ``ConflictError``.
* R12 — ``soft_delete_owned`` happy: row hidden from ``get_owned`` /
  ``list_owned`` afterward.
* R13 — ``soft_delete_owned`` wrong owner / already-deleted returns
  ``False``.

Note on ``updated_at`` for R8: the DB ``now()`` is
``transaction_timestamp()`` — constant within the SAVEPOINT fixture's
single transaction — so a create-then-update in one test cannot observe
``updated_at`` *advancing* past ``created_at`` (both resolve to the same
instant). R8 therefore asserts the ``version`` increment (the load-
bearing trigger check) and the field change, not a wall-clock advance;
the HTTP integration tests (separate requests → separate transactions)
cover observable ``updated_at`` movement.
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


# ---- R8 — update_owned real change (version +1, not +2) --------------


@pytest.mark.integration
async def test_r8_update_owned_real_change_bumps_version_by_one(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    created = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Before"))
    assert created.version == 1

    updated = await repo.update_owned(
        project_id=created.id,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        expected_version=1,
        changes={"name": "After", "description": "now set"},
    )

    assert updated is not None
    assert updated.name == "After"
    assert updated.description == "now set"
    # Load-bearing: the guarded trigger must NOT compound the hand-set +1.
    assert updated.version == 2
    # Re-fetch confirms persistence (not just the RETURNING projection).
    refetched = await repo.get_owned(
        project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id
    )
    assert refetched is not None
    assert refetched.name == "After"
    assert refetched.version == 2


# ---- R9 — update_owned version-stale ----------------------------------


@pytest.mark.integration
async def test_r9_update_owned_version_stale_returns_none_untouched(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    created = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Keep"))

    result = await repo.update_owned(
        project_id=created.id,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        expected_version=99,  # stale
        changes={"name": "ShouldNotApply"},
    )
    assert result is None

    refetched = await repo.get_owned(
        project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id
    )
    assert refetched is not None
    assert refetched.name == "Keep"  # untouched
    assert refetched.version == 1


# ---- R10 — update_owned wrong owner/tenant ----------------------------


@pytest.mark.integration
async def test_r10_update_owned_wrong_owner_returns_none_untouched(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    created = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Owned"))

    # Wrong owner → no match.
    assert (
        await repo.update_owned(
            project_id=created.id,
            tenant_id=tenant_id,
            owner_user_id=uuid4(),
            expected_version=1,
            changes={"name": "Hijacked"},
        )
    ) is None
    # Wrong tenant → no match.
    assert (
        await repo.update_owned(
            project_id=created.id,
            tenant_id=uuid4(),
            owner_user_id=owner_id,
            expected_version=1,
            changes={"name": "Hijacked"},
        )
    ) is None

    refetched = await repo.get_owned(
        project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id
    )
    assert refetched is not None
    assert refetched.name == "Owned"
    assert refetched.version == 1


# ---- R11 — update_owned rename collision → ConflictError --------------


@pytest.mark.integration
async def test_r11_update_owned_rename_collision_raises_conflict(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    a = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Alpha"))
    await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Beta"))

    with pytest.raises(ConflictError):
        await repo.update_owned(
            project_id=a.id,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            expected_version=1,
            changes={"name": "Beta"},  # collides with the live "Beta"
        )


# ---- R12 — soft_delete_owned happy ------------------------------------


@pytest.mark.integration
async def test_r12_soft_delete_owned_hides_from_get_and_list(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    created = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Doomed"))

    ok = await repo.soft_delete_owned(
        project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id
    )
    assert ok is True

    # Hidden from the scoped read + list afterward.
    assert (
        await repo.get_owned(project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id)
    ) is None
    rows = await repo.list_owned(tenant_id=tenant_id, owner_user_id=owner_id, limit=20)
    assert created.id not in {r.id for r in rows}


# ---- R13 — soft_delete_owned wrong owner / already-deleted ------------


@pytest.mark.integration
async def test_r13_soft_delete_owned_wrong_owner_or_repeat_returns_false(
    session: AsyncSession,
) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    repo = ProjectRepository(session)
    created = await repo.add(_entity(tenant_id=tenant_id, owner_user_id=owner_id, name="Target"))

    # Wrong owner → no live owned row matched.
    assert (
        await repo.soft_delete_owned(
            project_id=created.id, tenant_id=tenant_id, owner_user_id=uuid4()
        )
    ) is False

    # First real delete succeeds; the repeat finds no live row → False.
    assert (
        await repo.soft_delete_owned(
            project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id
        )
    ) is True
    assert (
        await repo.soft_delete_owned(
            project_id=created.id, tenant_id=tenant_id, owner_user_id=owner_id
        )
    ) is False
