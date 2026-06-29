"""AI model registry, pricing, provider plugin registrations (CR-11).

Schema reference: ``docs/database/schema.md`` §15.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import model_status_enum, plugin_kind_enum, pricing_unit_enum
from app.infrastructure.db.mixins import (
    CreatedAtOnlyMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class AIModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_models"

    model_key: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(plugin_kind_enum, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    modalities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    max_output_pixels: Mapped[int | None] = mapped_column(BigInteger)
    max_output_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        model_status_enum, nullable=False, server_default=text("'available'")
    )
    released_at: Mapped[date | None] = mapped_column(Date)
    deprecated_at: Mapped[date | None] = mapped_column(Date)
    retires_at: Mapped[date | None] = mapped_column(Date)
    successor_model_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="SET NULL"),
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint("model_key", name="uq_ai_models_model_key"),
        CheckConstraint(
            "deprecated_at IS NULL OR released_at IS NULL OR deprecated_at >= released_at",
            name="deprecated_after_release",
        ),
        CheckConstraint(
            "retires_at IS NULL OR deprecated_at IS NULL OR retires_at >= deprecated_at",
            name="retires_after_deprecation",
        ),
        CheckConstraint("id <> successor_model_id", name="no_self_successor"),
        Index("ix_ai_models_provider_kind_status", "provider", "kind", "status"),
        Index("ix_ai_models_successor_model_id", "successor_model_id"),
        # gin index on capabilities is emitted by baseline migration directly
    )


class AIModelPricing(UUIDPrimaryKeyMixin, CreatedAtOnlyMixin, Base):
    """Immutable: every change inserts a new row with new effective_from."""

    __tablename__ = "ai_model_pricing"

    model_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unit: Mapped[str] = mapped_column(pricing_unit_enum, nullable=False)
    price_per_unit: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    __table_args__ = (
        CheckConstraint("price_per_unit >= 0", name="price_nonnegative"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_to_after_from",
        ),
        Index(
            "ix_ai_model_pricing_model_id_effective_from",
            "model_id",
            "effective_from",
        ),
        Index(
            "uq_ai_model_pricing_model_id_unit",
            "model_id",
            "unit",
            unique=True,
            postgresql_where=text("effective_to IS NULL"),
        ),
    )


class ProviderPluginRegistration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "provider_plugin_registrations"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(plugin_kind_enum, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_health_status: Mapped[str | None] = mapped_column(Text)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_provider_plugin_registrations_name_version"),
        Index("ix_provider_plugin_registrations_kind_enabled", "kind", "enabled"),
    )


__all__ = ["AIModel", "AIModelPricing", "ProviderPluginRegistration"]
