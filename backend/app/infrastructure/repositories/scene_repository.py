"""SQLAlchemy implementation of ``ISceneRepository`` (Slice α5c).

Scenes live under a project's **implicit default storyboard**
(``Project → Storyboard → Scene`` — SCENE_AGGREGATE §2). This adapter hides
that intermediary: callers address scenes by ``(project_id, scene_id)`` and
never see ``storyboard_id`` or the raw sparse ordering key ``scene_number``.

Two concurrency-sensitive operations serialize on a ``SELECT … FOR UPDATE``
lock of the parent ``projects`` row (α5c D9) — the only lock available,
since ``storyboards`` has no per-project uniqueness and ``scenes`` no
per-storyboard ordering lock:

* ``ensure_default_storyboard`` — get-or-create exactly one default
  storyboard per project (guards the first-scene race).
* ``add`` / ``reorder_owned`` — ``scene_number`` assignment / rebalance
  (guards concurrent numbering).

Ordering (α5c D3/D10/D12): ``scene_number`` is a sparse gap key (1000, 2000,
…). ``add`` appends at ``max + 1000``. ``reorder_owned`` places a scene at
the gap midpoint between its target neighbours, falling back to a full
1000-step rebalance when no integer gap remains. Display ``position`` is the
dense 1-based rank derived from the sort — never the raw ``scene_number``.

Mutations (``update_owned`` / ``reorder_owned``) are the α4/α5b
version-fenced CAS (``UPDATE … WHERE version = :expected``), hand-setting
``version = SceneRow.version + 1`` exactly like
``ProjectRepository.update_owned``: the baseline ``bump_version()`` trigger
is **guarded** (``IF NEW.version = OLD.version``), so the hand-set ``+1``
makes it no-op and the net increment is exactly +1.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import ISceneRepository
from app.domain.scenes.scene import Scene as SceneEntity
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.scenes import (
    Scene as SceneRow,
    Storyboard as StoryboardRow,
)

# Sparse ordering step: appends and rebalances place scenes 1000 apart so
# up to ~10 midpoint inserts fit between any pair before a rebalance
# (α5c D3/D10).
_STEP = 1000


class SceneRepository(ISceneRepository):
    """Scene persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- helpers --------------------------------------------------------

    def _live_storyboard_ids(self, project_id: UUID) -> Select[tuple[UUID]]:
        """Sub-select of the project's live storyboard ids (scoping predicate)."""
        return (
            select(StoryboardRow.id)
            .where(StoryboardRow.project_id == project_id)
            .where(StoryboardRow.deleted_at.is_(None))
        )

    async def _lock_project(self, project_id: UUID) -> None:
        """Take a row lock on the parent project (held for the transaction).

        Serializes default-storyboard creation and ``scene_number``
        assignment (α5c D9). Ownership was already checked upstream via
        ``IProjectRepository.get_owned``; this lock only orders writers.
        """
        await self._session.execute(
            select(ProjectRow.id).where(ProjectRow.id == project_id).with_for_update()
        )

    async def _default_storyboard_id(self, project_id: UUID) -> UUID | None:
        """Return the project's earliest live storyboard id, or ``None`` (read-only)."""
        stmt = (
            select(StoryboardRow.id)
            .where(StoryboardRow.project_id == project_id)
            .where(StoryboardRow.deleted_at.is_(None))
            .order_by(StoryboardRow.created_at.asc(), StoryboardRow.id.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ---- α5c: storyboard + create --------------------------------------

    async def ensure_default_storyboard(self, project_id: UUID) -> tuple[UUID, bool]:
        await self._lock_project(project_id)
        existing = await self._default_storyboard_id(project_id)
        if existing is not None:
            return existing, False
        row = StoryboardRow(project_id=project_id, generated_by="system")
        self._session.add(row)
        await self._session.flush()
        return row.id, True

    async def add(
        self,
        *,
        storyboard_id: UUID,
        title: str,
        duration_seconds: float,
        narration: str | None,
        subtitle: str | None,
    ) -> SceneEntity:
        # Append: next number = current max live number + step (or the first
        # slot). Race-free because the caller holds the project-row lock via
        # a prior ``ensure_default_storyboard`` in the same transaction.
        max_num = (
            await self._session.execute(
                select(func.max(SceneRow.scene_number))
                .where(SceneRow.storyboard_id == storyboard_id)
                .where(SceneRow.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        next_num = _STEP if max_num is None else int(max_num) + _STEP
        row = SceneRow(
            storyboard_id=storyboard_id,
            scene_number=next_num,
            title=title,
            duration_seconds=duration_seconds,
            narration=narration,
            subtitle=subtitle,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _row_to_entity(row)

    # ---- α5c: reads -----------------------------------------------------

    async def list_by_project(self, project_id: UUID) -> list[SceneEntity]:
        # Read-only: never creates a storyboard (α5c D8). No storyboard yet →
        # no scenes.
        storyboard_id = await self._default_storyboard_id(project_id)
        if storyboard_id is None:
            return []
        stmt = (
            select(SceneRow)
            .where(SceneRow.storyboard_id == storyboard_id)
            .where(SceneRow.deleted_at.is_(None))
            .order_by(SceneRow.scene_number.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_entity(r) for r in rows]

    async def get_owned_scene(self, project_id: UUID, scene_id: UUID) -> SceneEntity | None:
        stmt = (
            select(SceneRow)
            .where(SceneRow.id == scene_id)
            .where(SceneRow.deleted_at.is_(None))
            .where(SceneRow.storyboard_id.in_(self._live_storyboard_ids(project_id)))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def position_of(self, storyboard_id: UUID, scene_number: int) -> int:
        count = (
            await self._session.execute(
                select(func.count())
                .select_from(SceneRow)
                .where(SceneRow.storyboard_id == storyboard_id)
                .where(SceneRow.deleted_at.is_(None))
                .where(SceneRow.scene_number < scene_number)
            )
        ).scalar_one()
        return int(count) + 1

    # ---- α5c: mutations -------------------------------------------------

    async def update_owned(
        self,
        project_id: UUID,
        scene_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> SceneEntity | None:
        # Version-fenced CAS on content columns only (never ``scene_number``
        # — ordering is ``reorder_owned``'s job, α5c D11). Hand-set
        # ``version + 1`` over the guarded trigger → net +1 (mirrors
        # ``ProjectRepository.update_owned``). Scoped to the project via the
        # storyboard sub-select so a scene of another project is untouched.
        assert changes, "update_owned requires at least one changed column"
        upd = (
            update(SceneRow)
            .where(SceneRow.id == scene_id)
            .where(SceneRow.version == expected_version)
            .where(SceneRow.deleted_at.is_(None))
            .where(SceneRow.storyboard_id.in_(self._live_storyboard_ids(project_id)))
            .values(**changes, version=SceneRow.version + 1, updated_at=func.now())
            .returning(SceneRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def soft_delete_owned(self, project_id: UUID, scene_id: UUID) -> bool:
        # Owner (project) scoped soft delete; no version fence (α5c D13).
        # Leaves a gap in ``scene_number`` — position is recomputed
        # dynamically, so no neighbour renumber is needed.
        stmt = (
            update(SceneRow)
            .where(SceneRow.id == scene_id)
            .where(SceneRow.deleted_at.is_(None))
            .where(SceneRow.storyboard_id.in_(self._live_storyboard_ids(project_id)))
            .values(deleted_at=func.now())
            .returning(SceneRow.id)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        return marked is not None

    async def reorder_owned(
        self,
        project_id: UUID,
        scene_id: UUID,
        target_position: int,
        expected_version: int,
    ) -> SceneEntity | None:
        # Serialize ordering against concurrent create/reorder (α5c D9).
        await self._lock_project(project_id)

        moved = (
            await self._session.execute(
                select(SceneRow)
                .where(SceneRow.id == scene_id)
                .where(SceneRow.deleted_at.is_(None))
                .where(SceneRow.storyboard_id.in_(self._live_storyboard_ids(project_id)))
            )
        ).scalar_one_or_none()
        # Fence here too: a concurrent content-PATCH does NOT take the project
        # lock, so it can bump ``version`` between the use case's
        # ``get_owned_scene`` and this call. A stale/absent row → None → 412.
        if moved is None or moved.version != expected_version:
            return None

        storyboard_id = moved.storyboard_id
        rows = list(
            (
                await self._session.execute(
                    select(SceneRow)
                    .where(SceneRow.storyboard_id == storyboard_id)
                    .where(SceneRow.deleted_at.is_(None))
                    .order_by(SceneRow.scene_number.asc())
                )
            )
            .scalars()
            .all()
        )
        ordered_ids = [r.id for r in rows]
        others = [r for r in rows if r.id != scene_id]
        n = len(others)

        # Clamp the requested 1-based position into the valid insert range.
        k = target_position - 1
        k = max(0, min(k, n))

        # No-op: the scene already sits at the requested slot → no write.
        current_index = ordered_ids.index(scene_id)
        if current_index == k:
            return _row_to_entity(moved)

        left = others[k - 1].scene_number if k > 0 else None
        right = others[k].scene_number if k < n else None
        new_number = _gap(left, right)

        if new_number is not None:
            upd = (
                update(SceneRow)
                .where(SceneRow.id == scene_id)
                .where(SceneRow.version == expected_version)
                .where(SceneRow.deleted_at.is_(None))
                .values(
                    scene_number=new_number,
                    version=SceneRow.version + 1,
                    updated_at=func.now(),
                )
                .returning(SceneRow)
            )
            row = (await self._session.execute(upd)).scalar_one_or_none()
            return _row_to_entity(row) if row is not None else None

        # No integer gap remains → rebalance the whole storyboard to fresh
        # 1000-step numbers (α5c D12).
        max_num = int(rows[-1].scene_number)
        return await self._rebalance(
            storyboard_id=storyboard_id,
            scene_id=scene_id,
            expected_version=expected_version,
            others=others,
            k=k,
            max_num=max_num,
        )

    async def _rebalance(
        self,
        *,
        storyboard_id: UUID,
        scene_id: UUID,
        expected_version: int,
        others: list[SceneRow],
        k: int,
        max_num: int,
    ) -> SceneEntity | None:
        # Phase A — shove every OTHER live scene into a temporary high range
        # (single statement → preserves uniqueness, no mid-update collision).
        # The offset clears the low 1000-step slots we are about to assign.
        offset = max_num + _STEP
        await self._session.execute(
            update(SceneRow)
            .where(SceneRow.storyboard_id == storyboard_id)
            .where(SceneRow.deleted_at.is_(None))
            .where(SceneRow.id != scene_id)
            .values(scene_number=SceneRow.scene_number + offset)
        )

        # Phase B — assign the moved scene its final slot UNDER the version
        # fence (a concurrent content-PATCH bump aborts the whole reorder →
        # the use case declines to commit → rollback undoes phase A).
        moved_final = (k + 1) * _STEP
        moved_row = (
            await self._session.execute(
                update(SceneRow)
                .where(SceneRow.id == scene_id)
                .where(SceneRow.version == expected_version)
                .where(SceneRow.deleted_at.is_(None))
                .values(
                    scene_number=moved_final,
                    version=SceneRow.version + 1,
                    updated_at=func.now(),
                )
                .returning(SceneRow)
            )
        ).scalar_one_or_none()
        if moved_row is None:
            return None

        # Assign the others their final low slots (skipping the moved scene's
        # index ``k``). Their ``version`` is trigger-bumped (α5c D14 —
        # accepted; the trigger owns their bump since we do not touch it).
        for i, other in enumerate(others):
            final_index = i if i < k else i + 1
            await self._session.execute(
                update(SceneRow)
                .where(SceneRow.id == other.id)
                .values(scene_number=(final_index + 1) * _STEP)
            )
        return _row_to_entity(moved_row)


def _gap(left: int | None, right: int | None) -> int | None:
    """Return an integer strictly between ``left`` and ``right``, or ``None``.

    ``None`` means "no integer room" → the caller rebalances. Endpoints:
    ``left is None`` = insert before the first scene; ``right is None`` =
    append after the last.
    """
    if left is None and right is None:
        return _STEP
    if left is None:
        assert right is not None
        candidate = right - _STEP
        if candidate < 1:
            candidate = right // 2
        if candidate < 1 or candidate >= right:
            return None
        return candidate
    if right is None:
        return left + _STEP
    if right - left <= 1:
        return None
    return (left + right) // 2


def _row_to_entity(row: SceneRow) -> SceneEntity:
    return SceneEntity(
        id=row.id,
        storyboard_id=row.storyboard_id,
        scene_number=row.scene_number,
        title=row.title,
        # ``duration_seconds`` is ``Numeric(8,3)`` → psycopg returns a
        # ``Decimal``; the domain models it as ``float``.
        duration_seconds=float(row.duration_seconds),
        narration=row.narration,
        subtitle=row.subtitle,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )
