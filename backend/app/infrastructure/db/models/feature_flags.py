"""Feature flags and overrides (CR-9, CR-DB-4).

Schema reference: ``docs/database/schema.md`` §24.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
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
from app.infrastructure.db.enums import flag_scope_enum, flag_type_enum
from app.infrastructure.db.mixins import (
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class FeatureFlag(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    flag_type: Mapped[str] = mapped_column(flag_type_enum, nullable=False)
    default_value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'false'::jsonb")
    )
    rollout_percent: Mapped[int | None] = mapped_column(Integer)
    variants: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint("key", name="uq_feature_flags_key"),
        CheckConstraint(
            "rollout_percent IS NULL OR rollout_percent BETWEEN 0 AND 100",
            name="rollout_percent_range",
        ),
    )


class FeatureFlagOverride(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "feature_flag_overrides"

    feature_flag_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("feature_flags.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(flag_scope_enum, nullable=False)
    scope_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "feature_flag_id",
            "scope",
            "scope_id",
            name="uq_feature_flag_overrides_feature_flag_id_scope_scope_id",
        ),
        Index(
            "ix_feature_flag_overrides_scope_scope_id",
            "scope",
            "scope_id",
        ),
    )


__all__ = ["FeatureFlag", "FeatureFlagOverride"]
