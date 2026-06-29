"""Reusable column mixins for ORM models.

Per ``docs/database/NAMING_CONVENTIONS.md`` §5–§6:
  - every mutable table gets ``created_at`` / ``updated_at`` / ``deleted_at``,
  - mutable aggregate roots get an integer ``version`` column,
  - tenant-scoped tables get ``tenant_id`` (declared at the model level for
    explicit FK control).

Triggers (``tg_<table>_biu_touch_updated_at`` and ``tg_<table>_biu_version_bump``)
are created in the baseline migration, not here, so that the ORM does not
silently rely on Python-side defaults for these audit columns.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """``id uuid PRIMARY KEY DEFAULT gen_random_uuid()``"""

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid4,
    )


class TimestampMixin:
    """``created_at`` and ``updated_at`` both timestamptz NOT NULL."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class SoftDeleteMixin:
    """Nullable ``deleted_at`` for soft delete semantics."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class VersionMixin:
    """Optimistic concurrency control via monotonic ``version`` column."""

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        default=1,
    )


class CreatedAtOnlyMixin:
    """For immutable / append-only tables (no updated_at, no deleted_at)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
