"""SQLAlchemy implementation of ``IExportJobRepository`` (Slice α8.5a).

An **export job** is a user's request to transcode a completed render's master
``MediaAsset`` into one delivery encoding (ADR-0030). Close in shape to the α7.1 render-job
adapter but with export-specific twists:

* **Ownership is derived through the render job → project.** ``export_jobs`` carries no
  ``project_id`` / ``tenant_id``; every owner-facing read joins ``export_jobs → render_jobs``
  and filters on ``render_jobs.project_id`` (the use case established ownership first).
* **Self-versioned CAS.** The worker transitions fence on the job's ``status`` predicate and
  hand-set ``version = version + 1`` (net +1 over the guarded bump trigger), so a job
  canceled between read and CAS cannot be silently overwritten.
* **Idempotency backstop.** :meth:`add` maps the partial-unique
  ``uq_export_jobs_render_job_id_format_quality_orientation`` violation to ``ConflictError``;
  the use case resolves it by returning the existing active/fulfilled job (Fork E).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IExportJobRepository
from app.core.errors import ConflictError
from app.domain.export.export_job import ExportJob as ExportJobEntity, ExportJobClaim
from app.domain.export.export_status import ExportStatus
from app.infrastructure.db.models.jobs import ExportJob as ExportJobRow, RenderJob as RenderJobRow

# Statuses that occupy the partial-unique slot (mirrors the index predicate).
_ACTIVE_STATUSES = (
    ExportStatus.QUEUED.value,
    ExportStatus.RUNNING.value,
    ExportStatus.SUCCEEDED.value,
)


class ExportJobRepository(IExportJobRepository):
    """Export-job persistence adapter (render-scoped ownership, self-versioned)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- create --------------------------------------------------------

    async def add(
        self,
        *,
        render_job_id: UUID,
        requested_by_user_id: UUID,
        format: str,
        quality: str,
        orientation: str,
        status: str,
    ) -> ExportJobEntity:
        row = ExportJobRow(
            render_job_id=render_job_id,
            requested_by_user_id=requested_by_user_id,
            format=format,
            quality=quality,
            orientation=orientation,
            status=status,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on the partial-unique index → an active/fulfilled export for this
            # (render_job_id, format, quality, orientation) already exists. Surface as
            # ConflictError; the use case returns the existing job (Fork E).
            raise ConflictError(
                "an active export already exists for this render + encoding",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)

    # ---- reads ---------------------------------------------------------

    async def get_active(
        self,
        render_job_id: UUID,
        *,
        format: str,
        quality: str,
        orientation: str,
    ) -> ExportJobEntity | None:
        stmt = (
            select(ExportJobRow)
            .where(ExportJobRow.render_job_id == render_job_id)
            .where(ExportJobRow.format == format)
            .where(ExportJobRow.quality == quality)
            .where(ExportJobRow.orientation == orientation)
            .where(ExportJobRow.status.in_(_ACTIVE_STATUSES))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def get_owned(self, project_id: UUID, export_job_id: UUID) -> ExportJobEntity | None:
        stmt = (
            select(ExportJobRow)
            .join(RenderJobRow, ExportJobRow.render_job_id == RenderJobRow.id)
            .where(ExportJobRow.id == export_job_id)
            .where(RenderJobRow.project_id == project_id)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    # ---- worker-facing lifecycle transitions (α8.5a) -------------------

    async def list_claimable(self, *, limit: int) -> list[ExportJobClaim]:
        # FIFO claim scan across all projects: oldest queued first, total order
        # (created_at, id) ASC. Joins render_jobs to resolve the owning project_id.
        stmt = (
            select(ExportJobRow.id, RenderJobRow.project_id)
            .join(RenderJobRow, ExportJobRow.render_job_id == RenderJobRow.id)
            .where(ExportJobRow.status == ExportStatus.QUEUED.value)
            .order_by(ExportJobRow.created_at.asc(), ExportJobRow.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [ExportJobClaim(export_job_id=r[0], project_id=r[1]) for r in rows]

    async def mark_running(self, export_job_id: UUID) -> ExportJobEntity | None:
        upd = (
            update(ExportJobRow)
            .where(ExportJobRow.id == export_job_id)
            .where(ExportJobRow.status == ExportStatus.QUEUED.value)
            .values(
                status=ExportStatus.RUNNING.value,
                version=ExportJobRow.version + 1,
                updated_at=func.now(),
            )
            .returning(ExportJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def mark_succeeded(
        self,
        export_job_id: UUID,
        *,
        output_media_asset_id: UUID,
        file_size_bytes: int,
    ) -> ExportJobEntity | None:
        upd = (
            update(ExportJobRow)
            .where(ExportJobRow.id == export_job_id)
            .where(ExportJobRow.status == ExportStatus.RUNNING.value)
            .values(
                status=ExportStatus.SUCCEEDED.value,
                finished_at=func.now(),
                output_media_asset_id=output_media_asset_id,
                file_size_bytes=file_size_bytes,
                version=ExportJobRow.version + 1,
                updated_at=func.now(),
            )
            .returning(ExportJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def mark_failed(self, export_job_id: UUID) -> ExportJobEntity | None:
        upd = (
            update(ExportJobRow)
            .where(ExportJobRow.id == export_job_id)
            .where(ExportJobRow.status == ExportStatus.RUNNING.value)
            .values(
                status=ExportStatus.FAILED.value,
                finished_at=func.now(),
                version=ExportJobRow.version + 1,
                updated_at=func.now(),
            )
            .returning(ExportJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    # ---- download accounting (α8.5b.1) --------------------------------

    async def record_download(self, export_job_id: UUID) -> ExportJobEntity | None:
        # Telemetry only — NOT a lifecycle CAS: no ``version`` bump (W8.5b.3). Guarded on
        # ``status='succeeded'`` so a non-servable job is never counted. Called best-effort by
        # the download use case; a failure here is swallowed as telemetry loss.
        upd = (
            update(ExportJobRow)
            .where(ExportJobRow.id == export_job_id)
            .where(ExportJobRow.status == ExportStatus.SUCCEEDED.value)
            .values(
                download_count=ExportJobRow.download_count + 1,
                last_downloaded_at=func.now(),
                updated_at=func.now(),
            )
            .returning(ExportJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None


def _row_to_entity(row: ExportJobRow) -> ExportJobEntity:
    return ExportJobEntity(
        id=row.id,
        render_job_id=row.render_job_id,
        requested_by_user_id=row.requested_by_user_id,
        format=row.format,
        quality=row.quality,
        orientation=row.orientation,
        status=row.status,
        output_media_asset_id=row.output_media_asset_id,
        download_count=row.download_count,
        last_downloaded_at=row.last_downloaded_at,
        file_size_bytes=row.file_size_bytes,
        finished_at=row.finished_at,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``render_job_repository.py`` / ``media_repository.py``.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
