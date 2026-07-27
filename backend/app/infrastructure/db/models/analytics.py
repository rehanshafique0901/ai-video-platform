"""Analytics events (partitioned, immutable).

Schema reference: ``docs/database/schema.md`` §26.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base


class AnalyticsEvent(Base):
    """Append-only, partitioned monthly by ``occurred_at``."""

    __tablename__ = "analytics_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", "occurred_at", name="pk_analytics_events"),
        Index(
            "ix_analytics_events_tenant_id_occurred_at",
            "tenant_id",
            "occurred_at",
        ),
        Index("ix_analytics_events_event_name_occurred_at", "event_name", "occurred_at"),
        # α9.0: DB-enforced exactly-once for the outbox consumer (ADR-0048). Includes the
        # partition key ``occurred_at`` so the parent unique index is valid + auto-propagates.
        Index(
            "uq_analytics_events_source_event_id",
            "source_event_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("source_event_id IS NOT NULL"),
        ),
        # α9.0: owner-scoped read path for GET /analytics/summary.
        Index(
            "ix_analytics_events_user_id_occurred_at",
            "user_id",
            "occurred_at",
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        # gin index on properties is emitted by the baseline migration
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
    )
    user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    session_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # α9.0: the outbox ``event.id`` that produced this row — the exactly-once dedupe key
    # (ADR-0048). Nullable + no FK (the outbox is transient; this is a logical key).
    source_event_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    event_name: Mapped[str] = mapped_column(Text, nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = ["AnalyticsEvent"]
