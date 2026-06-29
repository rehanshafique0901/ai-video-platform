"""Transactional outbox + event log (CR-4).

Schema reference: ``docs/database/schema.md`` §27 (event_outbox) and §28 (event_log).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import CreatedAtOnlyMixin


class EventOutbox(CreatedAtOnlyMixin, Base):
    __tablename__ = "event_outbox"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'1.0'"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "ix_event_outbox_unpublished_occurred_at",
            "occurred_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index("ix_event_outbox_aggregate_type_aggregate_id", "aggregate_type", "aggregate_id"),
    )


class EventLog(Base):
    """Append-only, partitioned monthly by ``occurred_at`` (canonical event store)."""

    __tablename__ = "event_log"
    __table_args__ = (
        PrimaryKeyConstraint("id", "occurred_at", name="pk_event_log"),
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "occurred_at",
            name="uq_event_log_aggregate",
        ),
        Index("ix_event_log_event_type_occurred_at", "event_type", "occurred_at"),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'1.0'"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["EventOutbox", "EventLog"]
