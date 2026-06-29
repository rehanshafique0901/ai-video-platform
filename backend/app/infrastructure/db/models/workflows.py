"""Workflow runs, steps, checkpoints (CR-7).

Schema reference: ``docs/database/schema.md`` §16.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
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
from app.infrastructure.db.enums import step_status_enum, workflow_status_enum
from app.infrastructure.db.mixins import (
    CreatedAtOnlyMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"

    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_key: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(workflow_status_enum, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggered_by_user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_workflow_runs_project_id_idempotency_key"
        ),
        Index("ix_workflow_runs_project_id_status", "project_id", "status"),
        Index("ix_workflow_runs_workflow_key_workflow_version", "workflow_key", "workflow_version"),
    )


class WorkflowStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_steps"

    workflow_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(step_status_enum, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id",
            "step_index",
            name="uq_workflow_steps_workflow_run_id_step_index",
        ),
        Index("ix_workflow_steps_workflow_run_id_status", "workflow_run_id", "status"),
    )


class WorkflowCheckpoint(UUIDPrimaryKeyMixin, CreatedAtOnlyMixin, Base):
    """Append-only resume points (ADR-0014)."""

    __tablename__ = "workflow_checkpoints"

    workflow_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index(
            "ix_workflow_checkpoints_workflow_run_id_step_index",
            "workflow_run_id",
            "step_index",
        ),
    )


__all__ = ["WorkflowRun", "WorkflowStep", "WorkflowCheckpoint"]
