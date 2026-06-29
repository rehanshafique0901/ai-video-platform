"""Timeline, tracks, clips, transitions.

Schema reference: ``docs/database/schema.md`` §14.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from app.infrastructure.db.enums import track_kind_enum
from app.infrastructure.db.mixins import (
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class Timeline(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, VersionMixin, Base):
    __tablename__ = "timelines"

    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_version_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("project_versions.id", ondelete="SET NULL"),
    )
    duration_seconds: Mapped[float] = mapped_column(
        Numeric(10, 3), nullable=False, server_default=text("0")
    )
    aspect_ratio: Mapped[str] = mapped_column(Text, nullable=False)
    frame_rate: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    background_color: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'#000000'")
    )

    __table_args__ = (
        CheckConstraint("frame_rate BETWEEN 1 AND 240", name="frame_rate_range"),
        Index(
            "uq_timelines_project_id",
            "project_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class Track(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "tracks"

    timeline_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("timelines.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(track_kind_enum, nullable=False)
    z_index: Mapped[int] = mapped_column(Integer, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    name: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index(
            "uq_tracks_timeline_id_z_index",
            "timeline_id",
            "z_index",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_tracks_timeline_id_kind", "timeline_id", "kind"),
    )


class Transition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "transitions"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(
        Numeric(6, 3), nullable=False, server_default=text("0.5")
    )
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class Clip(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "clips"

    track_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
    )
    media_asset_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL"),
    )
    start_seconds: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    end_seconds: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    source_start_seconds: Mapped[float] = mapped_column(
        Numeric(10, 3), nullable=False, server_default=text("0")
    )
    source_end_seconds: Mapped[float] = mapped_column(
        Numeric(10, 3), nullable=False, server_default=text("0")
    )
    transition_in_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("transitions.id", ondelete="SET NULL"),
    )
    transition_out_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("transitions.id", ondelete="SET NULL"),
    )
    effects: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    volume: Mapped[float] = mapped_column(
        Numeric(4, 2), nullable=False, server_default=text("1.00")
    )
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        CheckConstraint("start_seconds >= 0", name="start_nonnegative"),
        CheckConstraint("end_seconds > start_seconds", name="end_after_start"),
        CheckConstraint("volume BETWEEN 0 AND 4", name="volume_range"),
        Index("ix_clips_track_id_start_seconds", "track_id", "start_seconds"),
        Index("ix_clips_media_asset_id", "media_asset_id"),
    )


__all__ = ["Timeline", "Track", "Transition", "Clip"]
