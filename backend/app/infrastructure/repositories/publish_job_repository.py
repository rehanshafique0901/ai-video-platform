"""SQLAlchemy implementation of ``IPublishJobRepository`` (Slice α8.6b).

A **publish job** is a user's request to upload one finished export-delivery ``MediaAsset``
(PUB-1) to one connected ``social_accounts`` destination (PUB-2). A faithful adaptation of
``export_job_repository`` (DQ8) with three publishing-specific twists:

* **Direct ownership.** ``publish_jobs`` carries ``tenant_id`` + ``requested_by_user_id``,
  so owner reads scope on both (no render-job join, unlike export).
* **Source resolution (PUB-1).** :meth:`resolve_source` reads the finished ``export_jobs``
  row and joins ``render_jobs → projects`` for owner scoping, yielding the owning
  ``project_id`` (DQ1) + the delivery ``output_media_asset_id`` a job will consume.
* **Scheduling + retries.** :meth:`list_claimable` filters on ``scheduled_at``;
  :meth:`mark_running` bumps ``attempt``; :meth:`reschedule_for_retry` requeues with a future
  ``scheduled_at`` (DQ6). Self-versioned CAS transitions hand-set ``version = version + 1``
  (net +1 over the guarded bump trigger).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IPublishJobRepository
from app.core.errors import ConflictError
from app.domain.publishing.content_package import ContentPackage
from app.domain.publishing.publish_job import (
    PublishJob as PublishJobEntity,
    PublishJobClaim,
    PublishSource,
)
from app.domain.publishing.publish_status import PublishStatus
from app.infrastructure.db.models.jobs import ExportJob as ExportJobRow, RenderJob as RenderJobRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.publishing import PublishJob as PublishJobRow

# Statuses that occupy the partial-unique slot (mirrors the index predicate).
_ACTIVE_STATUSES = (
    PublishStatus.QUEUED.value,
    PublishStatus.RUNNING.value,
    PublishStatus.SUCCEEDED.value,
)


class PublishJobRepository(IPublishJobRepository):
    """Publish-job persistence adapter (direct ownership, self-versioned)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- source resolution (PUB-1) -------------------------------------

    async def resolve_source(
        self, export_job_id: UUID, *, tenant_id: UUID, owner_user_id: UUID
    ) -> PublishSource | None:
        stmt = (
            select(
                ExportJobRow.id,
                RenderJobRow.project_id,
                ExportJobRow.output_media_asset_id,
                ExportJobRow.status,
            )
            .join(RenderJobRow, ExportJobRow.render_job_id == RenderJobRow.id)
            .join(ProjectRow, RenderJobRow.project_id == ProjectRow.id)
            .where(ExportJobRow.id == export_job_id)
            .where(ProjectRow.tenant_id == tenant_id)
            .where(ProjectRow.owner_user_id == owner_user_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return PublishSource(
            export_job_id=row[0],
            project_id=row[1],
            source_media_asset_id=row[2],
            export_status=row[3],
        )

    # ---- create --------------------------------------------------------

    async def add(
        self,
        *,
        tenant_id: UUID,
        requested_by_user_id: UUID,
        project_id: UUID,
        source_export_job_id: UUID,
        source_media_asset_id: UUID,
        social_account_id: UUID,
        platform: str,
        status: str,
        scheduled_at: datetime | None,
        content_package: ContentPackage,
        max_attempts: int,
    ) -> PublishJobEntity:
        row = PublishJobRow(
            tenant_id=tenant_id,
            requested_by_user_id=requested_by_user_id,
            project_id=project_id,
            source_export_job_id=source_export_job_id,
            source_media_asset_id=source_media_asset_id,
            social_account_id=social_account_id,
            platform=platform,
            status=status,
            scheduled_at=scheduled_at,
            max_attempts=max_attempts,
            content_package=content_package.to_dict(),
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on the partial-unique index → an active/fulfilled publish for this
            # (source_media_asset_id, social_account_id) already exists (DQ2). Surface as
            # ConflictError; the use case returns the existing job.
            raise ConflictError(
                "an active publish already exists for this artifact + destination",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)

    # ---- reads ---------------------------------------------------------

    async def get_active(
        self, *, source_media_asset_id: UUID, social_account_id: UUID
    ) -> PublishJobEntity | None:
        stmt = (
            select(PublishJobRow)
            .where(PublishJobRow.source_media_asset_id == source_media_asset_id)
            .where(PublishJobRow.social_account_id == social_account_id)
            .where(PublishJobRow.status.in_(_ACTIVE_STATUSES))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def get_owned(
        self, *, tenant_id: UUID, owner_user_id: UUID, publish_job_id: UUID
    ) -> PublishJobEntity | None:
        stmt = (
            select(PublishJobRow)
            .where(PublishJobRow.id == publish_job_id)
            .where(PublishJobRow.tenant_id == tenant_id)
            .where(PublishJobRow.requested_by_user_id == owner_user_id)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def list_for_owner(
        self, *, tenant_id: UUID, owner_user_id: UUID
    ) -> list[PublishJobEntity]:
        stmt = (
            select(PublishJobRow)
            .where(PublishJobRow.tenant_id == tenant_id)
            .where(PublishJobRow.requested_by_user_id == owner_user_id)
            .order_by(PublishJobRow.created_at.desc(), PublishJobRow.id.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_entity(r) for r in rows]

    # ---- worker-facing lifecycle transitions ---------------------------

    async def list_claimable(self, *, now: datetime, limit: int) -> list[PublishJobClaim]:
        # FIFO claim scan across all projects: oldest queued + due first. project_id is a
        # real column (DQ1) — no join needed to resolve the owning project.
        stmt = (
            select(PublishJobRow.id, PublishJobRow.project_id)
            .where(PublishJobRow.status == PublishStatus.QUEUED.value)
            .where((PublishJobRow.scheduled_at.is_(None)) | (PublishJobRow.scheduled_at <= now))
            .order_by(PublishJobRow.created_at.asc(), PublishJobRow.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [PublishJobClaim(publish_job_id=r[0], project_id=r[1]) for r in rows]

    async def mark_running(self, publish_job_id: UUID) -> PublishJobEntity | None:
        upd = (
            update(PublishJobRow)
            .where(PublishJobRow.id == publish_job_id)
            .where(PublishJobRow.status == PublishStatus.QUEUED.value)
            .values(
                status=PublishStatus.RUNNING.value,
                attempt=PublishJobRow.attempt + 1,
                version=PublishJobRow.version + 1,
                updated_at=func.now(),
            )
            .returning(PublishJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def mark_succeeded(
        self,
        publish_job_id: UUID,
        *,
        platform_post_id: str,
        platform_post_url: str | None,
    ) -> PublishJobEntity | None:
        upd = (
            update(PublishJobRow)
            .where(PublishJobRow.id == publish_job_id)
            .where(PublishJobRow.status == PublishStatus.RUNNING.value)
            .values(
                status=PublishStatus.SUCCEEDED.value,
                finished_at=func.now(),
                published_at=func.now(),
                platform_post_id=platform_post_id,
                platform_post_url=platform_post_url,
                error=None,
                version=PublishJobRow.version + 1,
                updated_at=func.now(),
            )
            .returning(PublishJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def mark_failed(
        self, publish_job_id: UUID, *, error: dict[str, Any]
    ) -> PublishJobEntity | None:
        upd = (
            update(PublishJobRow)
            .where(PublishJobRow.id == publish_job_id)
            .where(PublishJobRow.status == PublishStatus.RUNNING.value)
            .values(
                status=PublishStatus.FAILED.value,
                finished_at=func.now(),
                error=error,
                version=PublishJobRow.version + 1,
                updated_at=func.now(),
            )
            .returning(PublishJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def reschedule_for_retry(
        self, publish_job_id: UUID, *, scheduled_at: datetime, error: dict[str, Any]
    ) -> PublishJobEntity | None:
        upd = (
            update(PublishJobRow)
            .where(PublishJobRow.id == publish_job_id)
            .where(PublishJobRow.status == PublishStatus.RUNNING.value)
            .values(
                status=PublishStatus.QUEUED.value,
                scheduled_at=scheduled_at,
                error=error,
                version=PublishJobRow.version + 1,
                updated_at=func.now(),
            )
            .returning(PublishJobRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None


def _row_to_entity(row: PublishJobRow) -> PublishJobEntity:
    return PublishJobEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        requested_by_user_id=row.requested_by_user_id,
        project_id=row.project_id,
        source_export_job_id=row.source_export_job_id,
        source_media_asset_id=row.source_media_asset_id,
        social_account_id=row.social_account_id,
        platform=row.platform,
        status=row.status,
        scheduled_at=row.scheduled_at,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        content_package=ContentPackage.from_dict(row.content_package),
        platform_post_id=row.platform_post_id,
        platform_post_url=row.platform_post_url,
        error=row.error,
        published_at=row.published_at,
        finished_at=row.finished_at,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
