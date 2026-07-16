"""SQLAlchemy implementation of ``IRenderJobRepository`` (Slice α7.1).

A **render job** is the request to render a project's timeline (ADR-0039). This
adapter is close in shape to the α6.2 media repository (no ordering key beyond
``created_at``, DB-owned timestamps) but with the α5a self-versioned OCC twist:

* **Project-scoped, not owner-scoped.** ``render_jobs`` carries no
  ``tenant_id`` / ``owner_user_id``; ownership is derived through the project.
  Every read/write filters on ``project_id`` (the use case established ownership
  first). There is **no** ``deleted_at`` — a job is a terminal audit record.
* **Self-versioned CAS.** :meth:`cancel` fences on the job's own ``version``
  (hand-set ``+1`` over the guarded ``tg_render_jobs_biu_version_bump`` trigger,
  net +1) AND on the ``status IN ('queued','running')`` predicate, so a worker
  completing the job between read and CAS cannot be silently overwritten (the
  terminal-state guard is race-safe at the DB, not just at the use case).
* **Idempotency backstop.** ``add`` maps the
  ``uq_render_jobs_project_id_idempotency_key`` violation to ``ConflictError``;
  the use case resolves it by returning the existing job (α7.1 Q4/D3.7).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IRenderJobRepository
from app.core.errors import ConflictError
from app.domain.render.render_job import RenderJob as RenderJobEntity
from app.domain.render.render_status import RenderStatus
from app.infrastructure.db.models.jobs import RenderJob as RenderJobRow

# The α7.1 cancel-eligible statuses — kept in one place so the CAS predicate and
# the domain enum cannot drift. Mirrors ``RenderStatus.is_cancelable``.
_CANCELABLE_STATUSES = (RenderStatus.QUEUED.value, RenderStatus.RUNNING.value)


class RenderJobRepository(IRenderJobRepository):
    """Render-job persistence adapter (project-scoped, self-versioned, no soft-delete)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- create --------------------------------------------------------

    async def add(
        self,
        *,
        project_id: UUID,
        timeline_id: UUID,
        pipeline: str,
        pipeline_version: str,
        queue: str,
        priority: int,
        status: str,
        idempotency_key: str | None,
    ) -> RenderJobEntity:
        row = RenderJobRow(
            project_id=project_id,
            timeline_id=timeline_id,
            pipeline=pipeline,
            pipeline_version=pipeline_version,
            queue=queue,
            priority=priority,
            status=status,
            idempotency_key=idempotency_key,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_render_jobs_project_id_idempotency_key`` → a job with
            # this (project_id, idempotency_key) already exists. Surface as
            # ConflictError; the use case maps it to "return the existing job"
            # (α7.1 Q4/D3.7). The unique constraint is the race-safe backstop
            # behind the use case's pre-check.
            raise ConflictError(
                "render job already exists for this idempotency key",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)

    # ---- reads ---------------------------------------------------------

    async def get_by_project_and_key(
        self, project_id: UUID, idempotency_key: str
    ) -> RenderJobEntity | None:
        stmt = (
            select(RenderJobRow)
            .where(RenderJobRow.project_id == project_id)
            .where(RenderJobRow.idempotency_key == idempotency_key)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def list_by_project(
        self,
        project_id: UUID,
        *,
        status: str | None = None,
    ) -> list[RenderJobEntity]:
        stmt = select(RenderJobRow).where(RenderJobRow.project_id == project_id)
        if status is not None:
            stmt = stmt.where(RenderJobRow.status == status)
        # Total order (created_at, id) DESC → stable newest-first, no dupes/skips
        # under timestamp ties (mirrors the media/project repositories).
        stmt = stmt.order_by(RenderJobRow.created_at.desc(), RenderJobRow.id.desc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_entity(r) for r in rows]

    async def get_owned(self, project_id: UUID, render_job_id: UUID) -> RenderJobEntity | None:
        stmt = (
            select(RenderJobRow)
            .where(RenderJobRow.id == render_job_id)
            .where(RenderJobRow.project_id == project_id)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    # ---- mutations -----------------------------------------------------

    async def cancel(
        self,
        project_id: UUID,
        render_job_id: UUID,
        expected_version: int,
    ) -> RenderJobEntity | None:
        # Version-fenced CAS with a race-safe terminal-state guard. The
        # ``status IN (...)`` predicate ensures a job that a worker moved to
        # ``succeeded``/``failed`` between the use case's read and this write is
        # NOT overwritten (RETURNING yields no row → None → the use case maps to
        # 409). ``version = version + 1`` is hand-set (net +1 over the guarded
        # trigger); ``updated_at`` co-set to now(). A None return is
        # disambiguated by the use case against its prior ``get_owned`` read.
        upd = (
            update(RenderJobRow)
            .where(RenderJobRow.id == render_job_id)
            .where(RenderJobRow.project_id == project_id)
            .where(RenderJobRow.version == expected_version)
            .where(RenderJobRow.status.in_(_CANCELABLE_STATUSES))
            .values(
                status=RenderStatus.CANCELED.value,
                version=RenderJobRow.version + 1,
                updated_at=func.now(),
            )
            .returning(RenderJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None


def _row_to_entity(row: RenderJobRow) -> RenderJobEntity:
    return RenderJobEntity(
        id=row.id,
        project_id=row.project_id,
        timeline_id=row.timeline_id,
        workflow_run_id=row.workflow_run_id,
        pipeline=row.pipeline,
        pipeline_version=row.pipeline_version,
        queue=row.queue,
        priority=row.priority,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        progress=row.progress,
        error=dict(row.error) if row.error is not None else None,
        output_media_asset_id=row.output_media_asset_id,
        idempotency_key=row.idempotency_key,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``project_repository.py`` / ``media_repository.py``.
    Duplicated rather than shared because a one-line ``getattr`` chain is cheaper
    to read inline than a shared infra-utility import.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
