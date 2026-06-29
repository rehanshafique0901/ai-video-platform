"""Storyboards, Scenes, Prompts.

Schema reference: ``docs/database/schema.md`` §10–§11.
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
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import prompt_kind_enum
from app.infrastructure.db.mixins import (
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class Storyboard(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, VersionMixin, Base):
    __tablename__ = "storyboards"

    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_version_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("project_versions.id", ondelete="SET NULL"),
    )
    generated_by: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("generated_by IN ('system','user')", name="generated_by_valid"),
        Index("ix_storyboards_project_id_created_at", "project_id", "created_at"),
    )


class Scene(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, VersionMixin, Base):
    __tablename__ = "scenes"

    storyboard_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("storyboards.id", ondelete="CASCADE"),
        nullable=False,
    )
    scene_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    narration: Mapped[str | None] = mapped_column(Text)
    subtitle: Mapped[str | None] = mapped_column(Text)
    emotion: Mapped[str | None] = mapped_column(Text)
    camera_angle: Mapped[str | None] = mapped_column(Text)
    camera_motion: Mapped[str | None] = mapped_column(Text)
    lens: Mapped[str | None] = mapped_column(Text)
    lighting: Mapped[str | None] = mapped_column(Text)
    weather: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    animation: Mapped[str | None] = mapped_column(Text)
    transition_in: Mapped[str | None] = mapped_column(Text)
    music_mood: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint("duration_seconds > 0", name="duration_positive"),
        Index(
            "uq_scenes_storyboard_id_scene_number",
            "storyboard_id",
            "scene_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_scenes_storyboard_id", "storyboard_id"),
    )


class Prompt(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "prompts"

    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    scene_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("scenes.id", ondelete="SET NULL"),
    )
    kind: Mapped[str] = mapped_column(prompt_kind_enum, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="SET NULL"),
    )
    generated_by_agent: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_prompts_project_id_kind", "project_id", "kind"),
        Index("ix_prompts_scene_id", "scene_id"),
        Index("ix_prompts_model_id", "model_id"),
    )


__all__ = ["Storyboard", "Scene", "Prompt"]
