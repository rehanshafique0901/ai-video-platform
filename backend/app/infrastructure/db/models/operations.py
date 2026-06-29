"""Idempotency keys (CR-DB-1) and distributed locks (CR-DB-2).

Schema reference: ``docs/database/schema.md`` §31–§32.
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import idempotency_status_enum
from app.infrastructure.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class IdempotencyKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "idempotency_keys"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_hash: Mapped[str | None] = mapped_column(Text)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(idempotency_status_enum, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Text)  # stored as text for portability
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "key",
            "resource_type",
            name="uq_idempotency_keys_tenant_id_key_resource_type",
        ),
        CheckConstraint(
            "(status = 'in_flight') = (response_hash IS NULL)",
            name="chk_idempotency_keys_response_hash_matches_status",
        ),
        Index(
            "ix_idempotency_keys_expires_at",
            "expires_at",
            postgresql_where=text("status <> 'in_flight'"),
        ),
        Index("ix_idempotency_keys_resource_type_resource_id", "resource_type", "resource_id"),
    )


class DistributedLock(Base):
    __tablename__ = "distributed_locks"

    lock_key: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (Index("ix_distributed_locks_lease_until", "lease_until"),)


__all__ = ["IdempotencyKey", "DistributedLock"]
