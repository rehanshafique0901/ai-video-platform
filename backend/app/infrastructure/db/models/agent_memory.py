"""Long-term agent memory (CR-3).

Schema reference: ``docs/database/schema.md`` §34.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover — guarded for environments without pgvector installed
    # See media.py for the rationale — mypy resolves ``Vector`` as a real type
    # from pgvector>=0.5, so the None fallback needs a narrow ignore.
    Vector = None  # type: ignore[assignment,misc]

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AgentMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_memory"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
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
    agent_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    if Vector is not None:
        embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    else:  # pragma: no cover
        embedding = None
    salience: Mapped[float] = mapped_column(
        Numeric(4, 3), nullable=False, server_default=text("0.5")
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index(
            "ix_agent_memory_tenant_id_agent_key_kind",
            "tenant_id",
            "agent_key",
            "kind",
        ),
        Index("ix_agent_memory_project_id", "project_id"),
        # HNSW vector index emitted by the baseline migration
    )


__all__ = ["AgentMemory"]
