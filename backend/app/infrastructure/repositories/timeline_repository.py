"""SQLAlchemy implementation of ``ITimelineRepository`` (Slice α6.3a).

The **Timeline aggregate** is a *self-contained OCC aggregate* (ADR-0038): the
``timelines`` root (1:1 with a project) is the only member with a ``version``
column (``VersionMixin`` + the guarded ``tg_timelines_biu_version_bump``
trigger). ``tracks`` (and — α6.3b — ``clips``) carry no version, so
``timelines.version`` is the **single OCC token** for the whole aggregate.

This adapter therefore has two flavours of write:

* **Root writes** — :meth:`update_owned` is the α4/α5b version-fenced CAS
  (``UPDATE … WHERE version = :expected``), hand-setting ``version + 1`` over the
  guarded trigger (net +1), exactly like ``ProjectRepository.update_owned``.
* **Child writes** — :meth:`add_track` / :meth:`update_track` /
  :meth:`soft_delete_track` do **not** carry a fence themselves (tracks have no
  version); the use case pairs each with :meth:`bump_version` — the aggregate
  roll-up — in the same transaction. :meth:`bump_version` is either fenced
  (``expected_version`` given → CAS → ``None`` on a stale token → ``412``) or
  unconditional (``None`` → child ``POST``; a create cannot be harmfully stale).

Ownership is derived through the project (the tables have no ``tenant_id`` /
``owner_user_id``); the use case has already gated on
``IProjectRepository.get_owned``, so this adapter scopes by ``project_id`` /
``timeline_id`` only. Soft-deleted rows are excluded. The aggregate never bumps
``projects.version`` and is never captured in ``project_versions`` (ADR-0035).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import ITimelineRepository
from app.core.errors import ConflictError
from app.domain.timeline.timeline import Timeline as TimelineEntity
from app.domain.timeline.track import Track as TrackEntity
from app.infrastructure.db.models.timeline import Timeline as TimelineRow, Track as TrackRow


class TimelineRepository(ITimelineRepository):
    """Timeline + track persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- timeline root -------------------------------------------------

    async def add(
        self,
        *,
        project_id: UUID,
        aspect_ratio: str,
        frame_rate: int,
        background_color: str,
    ) -> TimelineEntity:
        row = TimelineRow(
            project_id=project_id,
            aspect_ratio=aspect_ratio,
            frame_rate=frame_rate,
            background_color=background_color,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_timelines_project_id`` (partial, live rows) → the
            # project already has a live timeline. A project is 1:1 with its
            # timeline (Q3); surface as ConflictError so the use case maps it to
            # 409. The unique index is the race-safe backstop.
            raise ConflictError(
                "project already has a timeline",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _timeline_to_entity(row)

    async def get_by_project(self, project_id: UUID) -> TimelineEntity | None:
        stmt = (
            select(TimelineRow)
            .where(TimelineRow.project_id == project_id)
            .where(TimelineRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _timeline_to_entity(row) if row is not None else None

    async def update_owned(
        self,
        project_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> TimelineEntity | None:
        # Version-fenced CAS on the root's own columns. Hand-set ``version + 1``
        # over the guarded ``tg_timelines_biu_version_bump`` trigger (net +1),
        # mirroring ``ProjectRepository.update_owned``. A concurrent bump/delete
        # after the use case's ``get_by_project`` → zero rows → None → 412.
        assert changes, "update_owned requires at least one changed column"
        upd = (
            update(TimelineRow)
            .where(TimelineRow.project_id == project_id)
            .where(TimelineRow.version == expected_version)
            .where(TimelineRow.deleted_at.is_(None))
            .values(**changes, version=TimelineRow.version + 1, updated_at=func.now())
            .returning(TimelineRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _timeline_to_entity(row) if row is not None else None

    async def bump_version(
        self,
        project_id: UUID,
        expected_version: int | None,
    ) -> int | None:
        # Aggregate roll-up (ADR-0038 / Q13): advance ``timelines.version`` after a
        # real child (track/clip) mutation. Hand-set ``version + 1`` over the
        # guarded trigger (net +1), scoped project + live. When
        # ``expected_version`` is given, fence with ``WHERE version = :expected``
        # (stale → None → 412); when None, bump unconditionally (child POST — a
        # create cannot be harmfully stale). Never touches ``projects.version``.
        stmt = (
            update(TimelineRow)
            .where(TimelineRow.project_id == project_id)
            .where(TimelineRow.deleted_at.is_(None))
        )
        if expected_version is not None:
            stmt = stmt.where(TimelineRow.version == expected_version)
        stmt = stmt.values(version=TimelineRow.version + 1, updated_at=func.now()).returning(
            TimelineRow.version
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ---- tracks --------------------------------------------------------

    async def add_track(
        self,
        *,
        timeline_id: UUID,
        kind: str,
        z_index: int,
        name: str,
        locked: bool,
        muted: bool,
    ) -> TrackEntity:
        row = TrackRow(
            timeline_id=timeline_id,
            kind=kind,
            z_index=z_index,
            name=name,
            locked=locked,
            muted=muted,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_tracks_timeline_id_z_index`` (partial, live rows) →
            # another live track of this timeline holds ``z_index`` (Q5). Surface
            # as ConflictError → the use case maps it to 409.
            raise ConflictError(
                "track z_index already in use for this timeline",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _track_to_entity(row)

    async def list_tracks(self, timeline_id: UUID) -> list[TrackEntity]:
        stmt = (
            select(TrackRow)
            .where(TrackRow.timeline_id == timeline_id)
            .where(TrackRow.deleted_at.is_(None))
            .order_by(TrackRow.z_index.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_track_to_entity(r) for r in rows]

    async def get_track(self, timeline_id: UUID, track_id: UUID) -> TrackEntity | None:
        stmt = (
            select(TrackRow)
            .where(TrackRow.id == track_id)
            .where(TrackRow.timeline_id == timeline_id)
            .where(TrackRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _track_to_entity(row) if row is not None else None

    async def update_track(
        self,
        timeline_id: UUID,
        track_id: UUID,
        changes: Mapping[str, Any],
    ) -> TrackEntity | None:
        # No version fence (tracks have no OCC column — the parent timeline's
        # version is the aggregate token, bumped by the use case via
        # ``bump_version``). ``updated_at`` is trigger-owned. A z_index collision
        # with another live track raises ConflictError → 409.
        assert changes, "update_track requires at least one changed column"
        upd = (
            update(TrackRow)
            .where(TrackRow.id == track_id)
            .where(TrackRow.timeline_id == timeline_id)
            .where(TrackRow.deleted_at.is_(None))
            .values(**changes)
            .returning(TrackRow)
        )
        try:
            row = (await self._session.execute(upd)).scalar_one_or_none()
        except IntegrityError as e:
            raise ConflictError(
                "track z_index already in use for this timeline",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        return _track_to_entity(row) if row is not None else None

    async def soft_delete_track(self, timeline_id: UUID, track_id: UUID) -> bool:
        stmt = (
            update(TrackRow)
            .where(TrackRow.id == track_id)
            .where(TrackRow.timeline_id == timeline_id)
            .where(TrackRow.deleted_at.is_(None))
            .values(deleted_at=func.now())
            .returning(TrackRow.id)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        return marked is not None


def _timeline_to_entity(row: TimelineRow) -> TimelineEntity:
    return TimelineEntity(
        id=row.id,
        project_id=row.project_id,
        project_version_id=row.project_version_id,
        # ``duration_seconds`` is ``Numeric(10,3)`` → psycopg returns a
        # ``Decimal``; the domain models it as ``float``.
        duration_seconds=float(row.duration_seconds),
        aspect_ratio=row.aspect_ratio,
        frame_rate=row.frame_rate,
        background_color=row.background_color,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _track_to_entity(row: TrackRow) -> TrackEntity:
    return TrackEntity(
        id=row.id,
        timeline_id=row.timeline_id,
        kind=row.kind,
        z_index=row.z_index,
        locked=row.locked,
        muted=row.muted,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``project_repository.py`` / ``media_repository.py``.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
