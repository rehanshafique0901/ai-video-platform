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


def _num(value: Decimal | float | None) -> str | None:
    """Serialize a ``Numeric`` duration as a lossless string (or ``None``).

    JSONB round-trips floats lossily; a string preserves the exact scale the
    DB stored (e.g. ``"3.000"``) so a restored snapshot is byte-faithful
    (α5d Q7).
    """
    return None if value is None else str(value)


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
        "scenes": [
            {
                "id": str(s.id),
                "scene_number": s.scene_number,
                "title": s.title,
                "duration_seconds": _num(s.duration_seconds),
                "narration": s.narration,
                "subtitle": s.subtitle,
                "emotion": s.emotion,
                "camera_angle": s.camera_angle,
                "camera_motion": s.camera_motion,
                "lens": s.lens,
                "lighting": s.lighting,
                "weather": s.weather,
                "location": s.location,
                "animation": s.animation,
                "transition_in": s.transition_in,
                "music_mood": s.music_mood,
                "extra": s.extra,
            }
            for s in scenes
        ],
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
