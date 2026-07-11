"""Projects, Folders, Tags, ProjectVersion (CR-6).

Schema reference: ``docs/database/schema.md`` §6–§9.
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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import version_reason_enum
from app.infrastructure.db.mixins import (
    CreatedAtOnlyMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class Folder(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "folders"

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
        ForeignKey("folders.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("id <> parent_folder_id", name="no_self_parent"),
        Index(
            "uq_folders_parent_folder_id_name",
            "parent_folder_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_folders_tenant_id_parent_folder_id", "tenant_id", "parent_folder_id"),
    )


class Tag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tags"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tags_tenant_id_name"),)


class Project(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, VersionMixin, Base):
    __tablename__ = "projects"

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
    folder_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="SET NULL"),
    )
    current_version_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("project_versions.id", ondelete="SET NULL", use_alter=True),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    aspect_ratio: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 3))
    language: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'en'"))
    style: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "aspect_ratio IN ('horizontal','vertical','square')",
            name="aspect_ratio",
        ),
        Index(
            "uq_projects_tenant_id_owner_user_id_name",
            "tenant_id",
            "owner_user_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_projects_tenant_id_owner_user_id", "tenant_id", "owner_user_id"),
        Index(
            "ix_projects_folder_id",
            "folder_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # α5b (M3): composite partial index that directly serves
        # ``ProjectRepository.list_owned``'s keyset scan —
        # ``WHERE tenant_id=? AND owner_user_id=? AND deleted_at IS NULL
        # ORDER BY created_at DESC, id DESC``. The leading equality columns
        # (tenant_id, owner_user_id) narrow the scan; the trailing
        # ``created_at DESC, id DESC`` matches the ORDER BY so pagination
        # is an index-only range scan (no sort). Partial (``deleted_at IS
        # NULL``) so soft-deleted rows never bloat it. Created by migration
        # ``0008``; declared here so the schema validator (stage 8) expects
        # the name and no hardcoded count needs bumping. The older
        # ``ix_projects_tenant_id_owner_user_id`` is kept (α5b D10) — it may
        # serve future include-deleted admin/restore scans.
        Index(
            "ix_projects_owner_created_id",
            "tenant_id",
            "owner_user_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ProjectTag(Base):
    __tablename__ = "project_tags"

    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("ix_project_tags_tag_id_project_id", "tag_id", "project_id"),)


class ProjectVersion(UUIDPrimaryKeyMixin, CreatedAtOnlyMixin, Base):
    """Immutable. See ``schema.md`` §9 and ADR-0013."""

    __tablename__ = "project_versions"

    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("project_versions.id", ondelete="RESTRICT"),
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(version_reason_enum, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    diff_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_project_versions_project_id_version_number",
        ),
        CheckConstraint("id <> parent_version_id", name="no_self_parent"),
        Index(
            "ix_project_versions_project_id_created_at",
            "project_id",
            "created_at",
        ),
        Index("ix_project_versions_parent_version_id", "parent_version_id"),
    )


__all__ = ["Folder", "Tag", "Project", "ProjectTag", "ProjectVersion"]
