"""SQLAlchemy implementation of ``IProjectVersionRepository`` (Slice α5d).

Captures **immutable content snapshots** of a project plus its ordered
scenes into the ``project_versions`` ledger (``schema.md`` §9). The table is
append-only — a DB ``reject_mutation`` trigger (α5d DS7) rejects any UPDATE /
DELETE — so this adapter has exactly one write path (:meth:`create_snapshot`)
and two reads.

Snapshot assembly (α5d Q7 — canonical / restore-ready):

* Reads the live project, its **implicit default storyboard** (earliest live
  storyboard — the same one α5c auto-creates), and that storyboard's live
  scenes ordered by ``scene_number`` ASC.
* Denormalizes them into a self-describing JSONB blob carrying
  ``schema_version`` (Q10) so future snapshot shapes migrate cleanly.
* ``Numeric`` durations are emitted as **strings** (never lossy floats), UUIDs
  as strings — the whole blob is JSON-native so PostgreSQL stores it verbatim.
* Scene ``id`` values are preserved so a later restore can round-trip stable
  identities (α5c Q8 / α5d D-Q8).

Concurrency + bookkeeping (α5d Q6): the parent ``projects`` row is locked
``FOR UPDATE`` for the whole capture, so the ``MAX(version_number) + 1`` read
and insert cannot collide with a concurrent capture. ``parent_version_id`` is
set to the project's current ``current_version_id`` (``None`` for the first),
forming a linear lineage chain; after inserting the row we advance
``projects.current_version_id`` to the new version. That UPDATE does not touch
``projects.version``, so the guarded ``tg_projects_biu_version_bump`` trigger
bumps it by exactly +1 (the same guarded-trigger behaviour α5b/α5c rely on) —
a capture is a project mutation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IProjectVersionRepository
from app.domain.versions.project_version import (
    ProjectVersion as ProjectVersionEntity,
    ProjectVersionSummary,
)
from app.infrastructure.db.models.projects import (
    Project as ProjectRow,
    ProjectVersion as ProjectVersionRow,
)
from app.infrastructure.db.models.scenes import (
    Scene as SceneRow,
    Storyboard as StoryboardRow,
)

# Bump when the snapshot BODY shape changes (new/removed fields, restructured
# blocks). Consumers branch on it to migrate old blobs (α5d Q10). α5d.1 = 1.
_SNAPSHOT_SCHEMA_VERSION = 1


class ProjectVersionRepository(IProjectVersionRepository):
    """Project-version persistence adapter. Snapshots are append-only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- helpers --------------------------------------------------------

    async def _lock_project(self, project_id: UUID) -> ProjectRow | None:
        """Lock + fetch the live project row (``FOR UPDATE``), or ``None``.

        Ownership was already checked upstream via
        ``IProjectRepository.get_owned``; this lock only serializes concurrent
        captures so ``version_number`` assignment cannot collide (α5d Q6).
        """
        stmt = (
            select(ProjectRow)
            .where(ProjectRow.id == project_id)
            .where(ProjectRow.deleted_at.is_(None))
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _default_storyboard(self, project_id: UUID) -> StoryboardRow | None:
        """Return the project's earliest live storyboard, or ``None`` (read-only)."""
        stmt = (
            select(StoryboardRow)
            .where(StoryboardRow.project_id == project_id)
            .where(StoryboardRow.deleted_at.is_(None))
            .order_by(StoryboardRow.created_at.asc(), StoryboardRow.id.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _live_scenes(self, storyboard_id: UUID) -> list[SceneRow]:
        """Return the storyboard's live scenes ordered by ``scene_number`` ASC."""
        stmt = (
            select(SceneRow)
            .where(SceneRow.storyboard_id == storyboard_id)
            .where(SceneRow.deleted_at.is_(None))
            .order_by(SceneRow.scene_number.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # ---- α5d: capture ---------------------------------------------------

    async def create_snapshot(
        self,
        *,
        project_id: UUID,
        created_by_user_id: UUID,
        reason: str,
    ) -> ProjectVersionEntity:
        project = await self._lock_project(project_id)
        # The use case established ownership+liveness via ``get_owned`` before
        # calling us; a None here would mean a concurrent soft-delete slipped
        # in between. Treat it as "project gone" — the caller has already
        # committed to a capture, so surfacing it as a clean assertion is fine
        # (the outer transaction rolls back). In practice unreachable under the
        # project-row lock the use case's gate + this lock jointly provide.
        assert project is not None, "project vanished between ownership gate and capture"

        storyboard = await self._default_storyboard(project_id)
        scenes = await self._live_scenes(storyboard.id) if storyboard is not None else []

        # Next monotonic ordinal for THIS project (race-free under the lock).
        max_num = (
            await self._session.execute(
                select(func.max(ProjectVersionRow.version_number)).where(
                    ProjectVersionRow.project_id == project_id
                )
            )
        ).scalar_one_or_none()
        next_number = 1 if max_num is None else int(max_num) + 1

        # Lineage: the version that was current becomes this one's parent.
        parent_version_id = project.current_version_id

        snapshot = _build_snapshot(project, storyboard, scenes)

        row = ProjectVersionRow(
            project_id=project_id,
            version_number=next_number,
            parent_version_id=parent_version_id,
            created_by_user_id=created_by_user_id,
            reason=reason,
            snapshot=snapshot,
            diff_summary=None,
        )
        self._session.add(row)
        await self._session.flush()

        # Advance the current pointer. This UPDATE does NOT set ``version``, so
        # the guarded row trigger bumps ``projects.version`` by +1 (α5d Q6).
        await self._session.execute(
            update(ProjectRow).where(ProjectRow.id == project_id).values(current_version_id=row.id)
        )

        await self._session.refresh(row)
        return _row_to_entity(row)

    # ---- α5d: reads -----------------------------------------------------

    async def list_by_project(self, project_id: UUID) -> list[ProjectVersionSummary]:
        # Metadata-only projection (α5d Q4): never selects the snapshot /
        # diff_summary JSONB, so a long history stays cheap to list.
        stmt = (
            select(
                ProjectVersionRow.id,
                ProjectVersionRow.project_id,
                ProjectVersionRow.version_number,
                ProjectVersionRow.parent_version_id,
                ProjectVersionRow.created_by_user_id,
                ProjectVersionRow.reason,
                ProjectVersionRow.created_at,
            )
            .where(ProjectVersionRow.project_id == project_id)
            .order_by(ProjectVersionRow.version_number.desc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            ProjectVersionSummary(
                id=r.id,
                project_id=r.project_id,
                version_number=r.version_number,
                parent_version_id=r.parent_version_id,
                created_by_user_id=r.created_by_user_id,
                reason=r.reason,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def get_owned(self, project_id: UUID, version_id: UUID) -> ProjectVersionEntity | None:
        stmt = (
            select(ProjectVersionRow)
            .where(ProjectVersionRow.id == version_id)
            .where(ProjectVersionRow.project_id == project_id)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    # ---- α5d.2: restore -------------------------------------------------

    async def _ensure_default_storyboard(self, project_id: UUID) -> UUID:
        """Get-or-create the project's single default storyboard, return its id.

        Mirrors ``SceneRepository.ensure_default_storyboard`` (α5c D1/D9) but
        without the ``(id, created)`` tuple — restore only needs the target id.
        The parent ``projects`` row is already locked ``FOR UPDATE`` by the
        caller (``restore``), so this get-or-create is race-free.
        """
        existing = await self._session.execute(
            select(StoryboardRow.id)
            .where(StoryboardRow.project_id == project_id)
            .where(StoryboardRow.deleted_at.is_(None))
            .order_by(StoryboardRow.created_at.asc(), StoryboardRow.id.asc())
            .limit(1)
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            return found
        row = StoryboardRow(project_id=project_id, generated_by="system")
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def _reconcile_scenes(
        self, storyboard_id: UUID, snap_scenes: list[dict[str, Any]]
    ) -> None:
        """Make the live scene set equal ``snap_scenes``, keyed on ``id`` (Q3/Q4).

        Collision-proof strategy: **blanket soft-delete every live scene** under
        the storyboard first (empties the ``WHERE deleted_at IS NULL`` partial
        unique index on ``(storyboard_id, scene_number)``), then upsert each
        snapshot scene with its captured ``scene_number`` verbatim. Because the
        index is empty during the rewrite, permuted orderings (a move between
        capture and restore) cannot collide — a superset of the pre-flight's
        "soft-delete removed first" that also handles reordered survivors.
        Snapshot ids with an existing physical row (now soft-deleted) are
        **revived in place** (clear ``deleted_at``, rewrite columns — an INSERT
        would collide on the PK); ids with no physical row are **inserted** with
        the snapshot's UUID (identity preserved). Rows absent from the snapshot
        are left soft-deleted (the "removed" outcome). An empty snapshot leaves
        every live scene soft-deleted (empty project).
        """
        found = await self._session.execute(
            select(SceneRow.id).where(SceneRow.storyboard_id == storyboard_id)
        )
        existing_ids = set(found.scalars().all())

        # Blanket soft-delete all currently-live scenes → partial index emptied.
        await self._session.execute(
            update(SceneRow)
            .where(SceneRow.storyboard_id == storyboard_id)
            .where(SceneRow.deleted_at.is_(None))
            .values(deleted_at=func.now())
        )

        # Revive / insert each snapshot scene with its captured columns.
        for s in snap_scenes:
            sid = UUID(s["id"])
            values = _scene_write_values(s)
            if sid in existing_ids:
                await self._session.execute(
                    update(SceneRow).where(SceneRow.id == sid).values(**values, deleted_at=None)
                )
            else:
                self._session.add(SceneRow(id=sid, storyboard_id=storyboard_id, **values))
        await self._session.flush()

    async def restore(
        self,
        *,
        project_id: UUID,
        source_version_id: UUID,
        restored_by_user_id: UUID,
        expected_project_version: int,
    ) -> ProjectVersionEntity | None:
        project = await self._lock_project(project_id)
        # Ownership+liveness established upstream (projects.get_owned); a None
        # here would mean a concurrent soft-delete after the gate. Unreachable
        # under the joint lock; assert (outer transaction rolls back).
        assert project is not None, "project vanished between ownership gate and restore"

        # 1. Aggregate OCC fence (§4). Stale → 412, NO writes.
        if project.version != expected_project_version:
            return None

        # 2. Load the source snapshot (the use case already project-scoped +
        #    404-gated it; re-read under the lock for a consistent view).
        source = (
            await self._session.execute(
                select(ProjectVersionRow)
                .where(ProjectVersionRow.id == source_version_id)
                .where(ProjectVersionRow.project_id == project_id)
            )
        ).scalar_one_or_none()
        assert source is not None, "source version vanished between gate and restore"
        snapshot: dict[str, Any] = source.snapshot
        snap_project: dict[str, Any] = snapshot["project"]
        snap_scenes: list[dict[str, Any]] = snapshot.get("scenes", [])

        # 3. aspect_ratio is immutable (Q2/G6): assert-equal, never write. A
        #    mismatch is corruption, not a mutation — surface it, don't hide it.
        assert (
            snap_project["aspect_ratio"] == project.aspect_ratio
        ), "restore aspect_ratio mismatch — immutable column diverged from snapshot"

        # 4. Rehome under the live default storyboard (Q5), then reconcile.
        storyboard_id = await self._ensure_default_storyboard(project_id)
        await self._reconcile_scenes(storyboard_id, snap_scenes)

        # 5. Trailing capture: build the reason=restore snapshot from the
        #    now-restored live scenes + the (about-to-be-written) root, then
        #    assign the next ordinal and lineage parent = source (Q6/Q7).
        restored_scenes = await self._live_scenes(storyboard_id)
        new_project_version = expected_project_version + 1
        restore_snapshot = {
            "schema_version": _SNAPSHOT_SCHEMA_VERSION,
            "project": {**snap_project, "version": new_project_version},
            "storyboard": {"id": str(storyboard_id), "generated_by": "system"},
            "scenes": [_scene_dict(s) for s in restored_scenes],
        }
        max_num = (
            await self._session.execute(
                select(func.max(ProjectVersionRow.version_number)).where(
                    ProjectVersionRow.project_id == project_id
                )
            )
        ).scalar_one_or_none()
        next_number = 1 if max_num is None else int(max_num) + 1

        row = ProjectVersionRow(
            project_id=project_id,
            version_number=next_number,
            parent_version_id=source_version_id,
            created_by_user_id=restored_by_user_id,
            reason="restore",
            snapshot=restore_snapshot,
            diff_summary=None,
        )
        self._session.add(row)
        await self._session.flush()

        # 6. ONE projects UPDATE = exactly one aggregate version bump (Q6):
        #    rewrite the mutable root + advance current_version_id + hand-set
        #    version+1 in a single statement. Hand-setting +1 makes
        #    NEW.version != OLD.version, so the guarded trigger no-ops (net +1).
        #    aspect_ratio is deliberately absent (immutable, Q2).
        await self._session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == project_id)
            .values(
                name=snap_project["name"],
                description=snap_project["description"],
                duration_seconds=_to_numeric(snap_project["duration_seconds"]),
                language=snap_project["language"],
                style=snap_project["style"],
                settings=snap_project["settings"],
                current_version_id=row.id,
                version=ProjectRow.version + 1,
                updated_at=func.now(),
            )
        )

        await self._session.refresh(row)
        return _row_to_entity(row)


def _num(value: Decimal | float | None) -> str | None:
    """Serialize a ``Numeric`` duration as a lossless string (or ``None``).

    JSONB round-trips floats lossily; a string preserves the exact scale the
    DB stored (e.g. ``"3.000"``) so a restored snapshot is byte-faithful
    (α5d Q7).
    """
    return None if value is None else str(value)


def _to_numeric(value: str | None) -> Decimal | None:
    """Inverse of :func:`_num` — parse a snapshot duration string to ``Decimal``.

    Restore writes ``duration_seconds`` back into the ``Numeric`` column; going
    through ``Decimal`` (never ``float``) preserves the exact stored scale
    (``"3.000"`` → ``Decimal('3.000')``), so a capture → restore → re-capture
    round-trip is byte-faithful (α5d.2 R6).
    """
    return None if value is None else Decimal(value)


# The full ("fat") scene column set carried in a snapshot (α5c D2 / G4). Kept
# as one list so the snapshot serializer (:func:`_scene_dict`) and the restore
# writer (:func:`_scene_write_values`) can never drift apart.
_FAT_SCENE_COLUMNS: tuple[str, ...] = (
    "title",
    "narration",
    "subtitle",
    "emotion",
    "camera_angle",
    "camera_motion",
    "lens",
    "lighting",
    "weather",
    "location",
    "animation",
    "transition_in",
    "music_mood",
    "extra",
)


def _scene_dict(s: SceneRow) -> dict[str, Any]:
    """Serialize one scene row into the canonical snapshot shape (α5d Q7).

    ``id`` preserved verbatim (restore identity — G4), ``scene_number`` kept,
    duration as a lossless string, all fat cinematography columns included.
    """
    out: dict[str, Any] = {
        "id": str(s.id),
        "scene_number": s.scene_number,
        "duration_seconds": _num(s.duration_seconds),
    }
    for col in _FAT_SCENE_COLUMNS:
        out[col] = getattr(s, col)
    return out


def _scene_write_values(s: dict[str, Any]) -> dict[str, Any]:
    """Snapshot scene dict → ``scenes`` column values for a restore upsert.

    Inverse of :func:`_scene_dict`: rewrites ``scene_number`` verbatim (Q4) and
    the full fat column set (§6), converting the duration string back to
    ``Numeric`` via ``Decimal`` (R6). ``id`` / ``storyboard_id`` are set by the
    caller (identity + rehome target).
    """
    values: dict[str, Any] = {
        "scene_number": s["scene_number"],
        "duration_seconds": _to_numeric(s["duration_seconds"]),
    }
    for col in _FAT_SCENE_COLUMNS:
        values[col] = s[col]
    return values


def _build_snapshot(
    project: ProjectRow,
    storyboard: StoryboardRow | None,
    scenes: list[SceneRow],
) -> dict[str, Any]:
    """Assemble the canonical, JSON-native snapshot blob (α5d Q7/Q10).

    Shape (``schema_version`` = 1)::

        { "schema_version": 1,
          "project": { ...business columns... },
          "storyboard": { "id", "generated_by" } | null,
          "scenes": [ { ...fat scene columns, id preserved... }, ... ] }

    Scenes carry their full ("fat") column set — the physical schema keeps
    deferred cinematography fields (α5c D2) — so a snapshot is a complete,
    restore-ready record even though the α5c API exposes only a slim subset.
    """
    return {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "project": {
            "id": str(project.id),
            "name": project.name,
            "description": project.description,
            "aspect_ratio": project.aspect_ratio,
            "duration_seconds": _num(project.duration_seconds),
            "language": project.language,
            "style": project.style,
            "settings": project.settings,
            "version": project.version,
        },
        "storyboard": (
            None
            if storyboard is None
            else {"id": str(storyboard.id), "generated_by": storyboard.generated_by}
        ),
        "scenes": [_scene_dict(s) for s in scenes],
    }


def _row_to_entity(row: ProjectVersionRow) -> ProjectVersionEntity:
    return ProjectVersionEntity(
        id=row.id,
        project_id=row.project_id,
        version_number=row.version_number,
        parent_version_id=row.parent_version_id,
        created_by_user_id=row.created_by_user_id,
        reason=row.reason,
        snapshot=row.snapshot,
        diff_summary=row.diff_summary,
        created_at=row.created_at,
    )
