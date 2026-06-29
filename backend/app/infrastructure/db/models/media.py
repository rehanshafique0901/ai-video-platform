"""Media assets, library assets (CR-8), library folders.

Schema reference: ``docs/database/schema.md`` §12–§13.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover — guarded for environments without pgvector installed
    Vector = None

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import media_kind_enum, media_source_enum, storage_backend_enum
from app.infrastructure.db.mixins import (
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class MediaAsset(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "media_assets"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(media_kind_enum, nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
    )
    scene_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("scenes.id", ondelete="SET NULL"),
    )
    prompt_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("prompts.id", ondelete="SET NULL"),
    )
    model_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ai_models.id", ondelete="RESTRICT"),
    )
    provider: Mapped[str | None] = mapped_column(Text)
    storage_backend: Mapped[str] = mapped_column(storage_backend_enum, nullable=False)
    storage_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 3))
    checksum_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source: Mapped[str] = mapped_column(media_source_enum, nullable=False)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
        UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "storage_key",
            name="uq_media_assets_storage_backend_storage_bucket_storage_key",
        ),
        Index(
            "ix_media_assets_tenant_id_kind_created_at",
            "tenant_id",
            "kind",
            "created_at",
        ),
        Index("ix_media_assets_project_id", "project_id"),
        Index("ix_media_assets_prompt_id", "prompt_id"),
        Index("ix_media_assets_checksum_sha256", "checksum_sha256"),
    )


class LibraryFolder(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "library_folders"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_folder_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("library_folders.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("id <> parent_folder_id", name="no_self_parent"),
        Index(
            "uq_library_folders_parent_folder_id_name",
            "parent_folder_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_library_folders_tenant_id_parent_folder_id",
            "tenant_id",
            "parent_folder_id",
        ),
    )


class LibraryAsset(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, VersionMixin, Base):
    __tablename__ = "library_assets"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    library_folder_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("library_folders.id", ondelete="SET NULL"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    if Vector is not None:
        embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    else:  # pragma: no cover
        embedding = None
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("media_asset_id", name="uq_library_assets_media_asset_id"),
        Index("ix_library_assets_tenant_id_owner_user_id", "tenant_id", "owner_user_id"),
        Index(
            "ix_library_assets_last_used_at",
            "last_used_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # GIN on tags, HNSW on embedding are emitted by the baseline migration directly.
    )


class LibraryAssetProject(Base):
    __tablename__ = "library_asset_projects"

    library_asset_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("library_assets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    first_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = ["MediaAsset", "LibraryFolder", "LibraryAsset", "LibraryAssetProject"]
