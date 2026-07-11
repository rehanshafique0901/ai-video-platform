"""Render jobs and export jobs.

Schema reference: ``docs/database/schema.md`` §17–§18.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import (
    export_format_enum,
    export_orientation_enum,
    export_quality_enum,
    export_status_enum,
    render_status_enum,
)
from app.infrastructure.db.mixins import (
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class RenderJob(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "render_jobs"

    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    timeline_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("timelines.id", ondelete="RESTRICT"),
        nullable=False,
    )
    workflow_run_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="SET NULL"),
    )
    pipeline: Mapped[str] = mapped_column(Text, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    queue: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(render_status_enum, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stored as text (decimal-as-text, 0.00..100.00) per schema.md §17 —
    # see §37 Q7 for the retype-to-numeric(5,2) question, deferred until
    # profiling shows a need. The prior ``Mapped[float]`` annotation was
    # a drift bug: it lied about the runtime type SQLAlchemy loads.
    progress: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'0.00'"))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_media_asset_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL"),
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "queue IN ('critical','high','normal','low','background')",
            name="queue_valid",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_render_jobs_project_id_idempotency_key",
        ),
        Index(
            "ix_render_jobs_status_priority_created_at",
            "status",
            "priority",
            "created_at",
        ),
        Index("ix_render_jobs_project_id_status", "project_id", "status"),
    )


class ExportJob(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "export_jobs"

    render_job_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("render_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    format: Mapped[str] = mapped_column(export_format_enum, nullable=False)
    quality: Mapped[str] = mapped_column(export_quality_enum, nullable=False)
    orientation: Mapped[str] = mapped_column(export_orientation_enum, nullable=False)
    status: Mapped[str] = mapped_column(export_status_enum, nullable=False)
    output_media_asset_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL"),
    )
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_export_jobs_render_job_id", "render_job_id"),
        Index(
            "ix_export_jobs_requested_by_user_id_created_at",
            "requested_by_user_id",
            "created_at",
        ),
        # ADR-0030 (Phase 3 W1.1) — at most one active-or-fulfilled export per
        # (render_job_id, format, quality, orientation). `failed`/`canceled`
        # rows are excluded so retries after failure are permitted; `succeeded`
        # is included because export_jobs is the canonical artefact row
        # (download_count, last_downloaded_at, output_media_asset_id all live
        # on it). Mirrors migration 0003_export_jobs_partial_unique.
        Index(
            "uq_export_jobs_render_job_id_format_quality_orientation",
            "render_job_id",
            "format",
            "quality",
            "orientation",
            unique=True,
            postgresql_where=text("status IN ('queued','running','succeeded')"),
        ),
    )


__all__ = ["RenderJob", "ExportJob"]
