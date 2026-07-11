"""Integration tests for ``SceneRepository`` (Slice α5c).

Runs against the live database; each test is wrapped in a SAVEPOINT that
rolls back on teardown, so no rows persist. Covers the implicit-storyboard
get-or-create, gap-based append numbering, project-scoped reads, the
version-fenced content CAS (with the load-bearing anti-double-bump check),
soft delete, and the reorder gap/rebalance paths.

Note (transaction-constant ``now()``): the SAVEPOINT fixture holds one
transaction, so ``created_at`` / ``updated_at`` = ``transaction_timestamp()``
is constant. These tests assert on ``version`` increments, ``scene_number``
ordering, and content — never on wall-clock movement (that is HTTP-level,
separate transactions).

Coverage map (α5c pre-flight §5.2):

* R1 — ``ensure_default_storyboard`` creates one storyboard and is
  idempotent (second call → same id, ``created=False``).
* R2 — ``add`` appends at ``1000`` then ``2000`` (gap step), ``version==1``.
* R3 — ``list_by_project`` returns live scenes ordered by ``scene_number``;
  empty (and no storyboard created) when the project has none.
* R4 — ``get_owned_scene`` returns the scene; ``None`` for a foreign
  project and for a soft-deleted scene.
* R5 — ``update_owned`` real change bumps ``version`` by **exactly 1**
  (guarded trigger — hand-set +1 must not compound to +2).
* R6 — ``update_owned`` version-stale → ``None``, row untouched.
* R7 — ``update_owned`` foreign project → ``None`` (scoping).
* R8 — ``soft_delete_owned`` hides the scene; a second call → ``False``.
* R9 — ``reorder_owned`` within an available gap moves the scene and bumps
  its ``version`` by 1; the list order reflects the move.
* R10 — ``reorder_owned`` with no integer gap rebalances the whole
  storyboard to fresh 1000-step numbers and preserves the requested order.
* R11 — ``reorder_owned`` version-stale → ``None``, order untouched.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.scenes import (
    Scene as SceneRow,
    Storyboard as StoryboardRow,
)
from app.infrastructure.repositories.scene_repository import SceneRepository


async def _seed_owner(session: AsyncSession) -> tuple[UUID, UUID]:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="SR Test", slug=f"sr-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"sr-{user_id}@example.com",
            display_name="SR Owner",
        )
    )
    return tenant_id, user_id


async def _insert_project(session: AsyncSession, *, tenant_id: UUID, owner_user_id: UUID) -> UUID:
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=f"P {project_id}",
            aspect_ratio="horizontal",
        )
    )
    return project_id


async def _insert_packed_scenes(
    session: AsyncSession, *, project_id: UUID, numbers: list[int]
) -> tuple[UUID, list[UUID]]:
    """Insert a storyboard + scenes with EXPLICIT ``scene_number`` values.

    Used by the rebalance test (R10) to force a no-gap layout (adjacent
    integers) that ``add``'s ``max + 1000`` spacing never produces.
    """
    storyboard_id = uuid4()
    await session.execute(
        insert(StoryboardRow).values(id=storyboard_id, project_id=project_id, generated_by="system")
    )
    ids: list[UUID] = []
    for n in numbers:
        scene_id = uuid4()
        await session.execute(
            insert(SceneRow).values(
                id=scene_id,
                storyboard_id=storyboard_id,
                scene_number=n,
                title=f"S{n}",
                duration_seconds=1.0,
            )
        )
        ids.append(scene_id)
    await session.flush()
    return storyboard_id, ids


async def _live_numbers(session: AsyncSession, storyboard_id: UUID) -> list[tuple[UUID, int]]:
    result = await session.execute(
        select(SceneRow.id, SceneRow.scene_number)
        .where(SceneRow.storyboard_id == storyboard_id)
        .where(SceneRow.deleted_at.is_(None))
        .order_by(SceneRow.scene_number.asc())
    )
    return [(r[0], r[1]) for r in result.all()]


# ---- R1 — ensure_default_storyboard ----------------------------------


@pytest.mark.integration
async def test_r1_ensure_default_storyboard_is_idempotent(
    session: AsyncSession,
) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)

    sb1, created1 = await repo.ensure_default_storyboard(project_id)
    assert created1 is True
    sb2, created2 = await repo.ensure_default_storyboard(project_id)
    assert created2 is False
    assert sb1 == sb2


# ---- R2 — add gap numbering -------------------------------------------


@pytest.mark.integration
async def test_r2_add_appends_with_gap_step(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)
    sb, _ = await repo.ensure_default_storyboard(project_id)

    first = await repo.add(
        storyboard_id=sb,
        title="One",
        duration_seconds=1.0,
        narration=None,
        subtitle=None,
    )
    second = await repo.add(
        storyboard_id=sb,
        title="Two",
        duration_seconds=2.0,
        narration=None,
        subtitle=None,
    )
    assert first.scene_number == 1000
    assert second.scene_number == 2000
    assert first.version == 1
    assert second.version == 1


# ---- R3 — list ordered + empty ----------------------------------------


@pytest.mark.integration
async def test_r3_list_by_project_ordered_and_empty(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)

    # No storyboard yet → empty, and none created.
    assert await repo.list_by_project(project_id) == []
    assert await repo._default_storyboard_id(project_id) is None

    sb, _ = await repo.ensure_default_storyboard(project_id)
    a = await repo.add(
        storyboard_id=sb, title="A", duration_seconds=1.0, narration=None, subtitle=None
    )
    b = await repo.add(
        storyboard_id=sb, title="B", duration_seconds=1.0, narration=None, subtitle=None
    )
    scenes = await repo.list_by_project(project_id)
    assert [s.id for s in scenes] == [a.id, b.id]


# ---- R4 — get_owned_scene scoping -------------------------------------


@pytest.mark.integration
async def test_r4_get_owned_scene_scoping_and_soft_delete(
    session: AsyncSession,
) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    other_project = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)
    sb, _ = await repo.ensure_default_storyboard(project_id)
    scene = await repo.add(
        storyboard_id=sb,
        title="Mine",
        duration_seconds=1.0,
        narration=None,
        subtitle=None,
    )

    assert (await repo.get_owned_scene(project_id, scene.id)) is not None
    # Scene is not visible under a different project.
    assert (await repo.get_owned_scene(other_project, scene.id)) is None

    await repo.soft_delete_owned(project_id, scene.id)
    assert (await repo.get_owned_scene(project_id, scene.id)) is None


# ---- R5 — update real change (version +1, not +2) ---------------------


@pytest.mark.integration
async def test_r5_update_owned_real_change_bumps_version_by_one(
    session: AsyncSession,
) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)
    sb, _ = await repo.ensure_default_storyboard(project_id)
    scene = await repo.add(
        storyboard_id=sb,
        title="Before",
        duration_seconds=1.0,
        narration=None,
        subtitle=None,
    )

    updated = await repo.update_owned(
        project_id,
        scene.id,
        expected_version=1,
        changes={"title": "After", "subtitle": "sub"},
    )
    assert updated is not None
    assert updated.title == "After"
    assert updated.subtitle == "sub"
    # Load-bearing: guarded trigger must NOT compound the hand-set +1.
    assert updated.version == 2
    refetched = await repo.get_owned_scene(project_id, scene.id)
    assert refetched is not None
    assert refetched.version == 2
    assert refetched.title == "After"


# ---- R6 — update version-stale ----------------------------------------


@pytest.mark.integration
async def test_r6_update_owned_version_stale_returns_none(
    session: AsyncSession,
) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)
    sb, _ = await repo.ensure_default_storyboard(project_id)
    scene = await repo.add(
        storyboard_id=sb,
        title="Keep",
        duration_seconds=1.0,
        narration=None,
        subtitle=None,
    )

    result = await repo.update_owned(
        project_id, scene.id, expected_version=99, changes={"title": "Nope"}
    )
    assert result is None
    refetched = await repo.get_owned_scene(project_id, scene.id)
    assert refetched is not None
    assert refetched.title == "Keep"
    assert refetched.version == 1


# ---- R7 — update foreign project --------------------------------------


@pytest.mark.integration
async def test_r7_update_owned_foreign_project_returns_none(
    session: AsyncSession,
) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    other_project = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)
    sb, _ = await repo.ensure_default_storyboard(project_id)
    scene = await repo.add(
        storyboard_id=sb,
        title="Safe",
        duration_seconds=1.0,
        narration=None,
        subtitle=None,
    )

    result = await repo.update_owned(
        other_project, scene.id, expected_version=1, changes={"title": "Hijack"}
    )
    assert result is None
    refetched = await repo.get_owned_scene(project_id, scene.id)
    assert refetched is not None
    assert refetched.title == "Safe"


# ---- R8 — soft delete idempotent --------------------------------------


@pytest.mark.integration
async def test_r8_soft_delete_owned_then_false(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)
    sb, _ = await repo.ensure_default_storyboard(project_id)
    scene = await repo.add(
        storyboard_id=sb,
        title="Doomed",
        duration_seconds=1.0,
        narration=None,
        subtitle=None,
    )

    assert (await repo.soft_delete_owned(project_id, scene.id)) is True
    assert (await repo.soft_delete_owned(project_id, scene.id)) is False
    assert await repo.list_by_project(project_id) == []


# ---- R9 — reorder within a gap ----------------------------------------


@pytest.mark.integration
async def test_r9_reorder_owned_within_gap(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)
    sb, _ = await repo.ensure_default_storyboard(project_id)
    a = await repo.add(
        storyboard_id=sb, title="A", duration_seconds=1.0, narration=None, subtitle=None
    )
    b = await repo.add(
        storyboard_id=sb, title="B", duration_seconds=1.0, narration=None, subtitle=None
    )
    c = await repo.add(
        storyboard_id=sb, title="C", duration_seconds=1.0, narration=None, subtitle=None
    )

    # Move A to position 3 (last). Gap between B(2000) and C(3000) is wide.
    moved = await repo.reorder_owned(project_id, a.id, target_position=3, expected_version=1)
    assert moved is not None
    assert moved.version == 2
    order = [s.id for s in await repo.list_by_project(project_id)]
    assert order == [b.id, c.id, a.id]


# ---- R10 — reorder forces rebalance -----------------------------------


@pytest.mark.integration
async def test_r10_reorder_owned_rebalances_when_no_gap(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    # Packed layout: adjacent integers leave no gap for a midpoint insert.
    storyboard_id, ids = await _insert_packed_scenes(
        session, project_id=project_id, numbers=[1, 2, 3]
    )
    repo = SceneRepository(session)

    # Move the last scene (number 3) to the front (position 1) → no gap →
    # full rebalance to fresh 1000-step numbers.
    moved = await repo.reorder_owned(project_id, ids[2], target_position=1, expected_version=1)
    assert moved is not None

    numbered = await _live_numbers(session, storyboard_id)
    order = [sid for sid, _ in numbered]
    numbers = [num for _, num in numbered]
    assert order == [ids[2], ids[0], ids[1]]  # requested order preserved
    # Re-spaced to 1000-step slots (no adjacency remains).
    assert numbers == [1000, 2000, 3000]


# ---- R11 — reorder version-stale --------------------------------------


@pytest.mark.integration
async def test_r11_reorder_owned_version_stale_returns_none(
    session: AsyncSession,
) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = SceneRepository(session)
    sb, _ = await repo.ensure_default_storyboard(project_id)
    a = await repo.add(
        storyboard_id=sb, title="A", duration_seconds=1.0, narration=None, subtitle=None
    )
    b = await repo.add(
        storyboard_id=sb, title="B", duration_seconds=1.0, narration=None, subtitle=None
    )

    result = await repo.reorder_owned(project_id, a.id, target_position=2, expected_version=99)
    assert result is None
    order = [s.id for s in await repo.list_by_project(project_id)]
    assert order == [a.id, b.id]  # untouched
