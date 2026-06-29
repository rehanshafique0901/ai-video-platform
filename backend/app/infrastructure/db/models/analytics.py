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
