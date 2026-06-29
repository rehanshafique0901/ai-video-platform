"""Usage records (partitioned, immutable) and cost reconciliations (CR-12).

Schema reference: ``docs/database/schema.md`` §19. Partition children are
created in the baseline migration, not here; SQLAlchemy only sees the parent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import pricing_unit_enum, usage_status_enum
from app.infrastructure.db.mixins import CreatedAtOnlyMixin


class UsageRecord(Base):
    """Append-only, partitioned monthly by ``occurred_at`` (ADR-0019)."""

    __tablename__ = "usage_records"
    __table_args__ = (
        PrimaryKeyConstraint("id", "occurred_at", name="pk_usage_records"),
        CheckConstraint("credits_consumed >= 0", name="credits_nonnegative"),
        CheckConstraint("estimated_cost >= 0", name="estimated_cost_nonnegative"),
        CheckConstraint("actual_cost IS NULL OR actual_cost >= 0", name="actual_cost_nonnegative"),
        Index(
            "ix_usage_records_tenant_id_occurred_at",
            "tenant_id",
            "occurred_at",
        ),
        Index("ix_usage_records_model_id_occurred_at", "model_id", "occurred_at"),
        Index("ix_usage_records_workflow_run_id", "workflow_run_id"),
        Index("ix_usage_records_request_id", "request_id"),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    project_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
    )
    scene_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("scenes.id", ondelete="SET NULL"),
    )
    prompt_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("prompts.id", ondelete="SET NULL"),
    )
    workflow_run_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="SET NULL"),
    )
    workflow_step_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_steps.id", ondelete="SET NULL"),
    )
    model_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pricing_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ai_model_pricing.id", ondelete="SET NULL"),
    )
    request_id: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(pricing_unit_enum, nullable=False)
    unit_count: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    tokens_prompt: Mapped[int | None] = mapped_column(Integer)
    tokens_completion: Mapped[int | None] = mapped_column(Integer)
    images_count: Mapped[int | None] = mapped_column(Integer)
    seconds_generated: Mapped[float | None] = mapped_column(Numeric(10, 3))
    credits_consumed: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, server_default=text("0")
    )
    estimated_cost: Mapped[float] = mapped_column(
        Numeric(18, 8), nullable=False, server_default=text("0")
    )
    actual_cost: Mapped[float | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(usage_status_enum, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CostReconciliation(CreatedAtOnlyMixin, Base):
    __tablename__ = "cost_reconciliations"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invoiced_amount: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    estimated_amount: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    variance: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("period_end > period_start", name="period_valid"),
        Index(
            "ix_cost_reconciliations_tenant_id_period_start",
            "tenant_id",
            "period_start",
        ),
    )


__all__ = ["UsageRecord", "CostReconciliation"]
