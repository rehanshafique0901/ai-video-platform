"""Immutable audit log (CR-DB-3), partitioned monthly.

Schema reference: ``docs/database/schema.md`` §33.
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
from app.infrastructure.db.enums import audit_actor_kind_enum


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        PrimaryKeyConstraint("id", "occurred_at", name="pk_audit_log"),
        Index(
            "ix_audit_log_tenant_id_occurred_at",
            "tenant_id",
            "occurred_at",
        ),
        Index(
            "ix_audit_log_entity_type_entity_id_occurred_at",
            "entity_type",
            "entity_id",
            "occurred_at",
        ),
        Index("ix_audit_log_actor_user_id_occurred_at", "actor_user_id", "occurred_at"),
        Index("ix_audit_log_action_occurred_at", "action", "occurred_at"),
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
    actor_kind: Mapped[str] = mapped_column(audit_actor_kind_enum, nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    actor_label: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    request_id: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = ["AuditLog"]
