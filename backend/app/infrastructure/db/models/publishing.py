"""Publishing bounded context — ORM models (α8.6a Account Connections).

Two tables, **profile separated from secret** (ADR-0047 R1):

- ``social_accounts``    — non-secret connection profile + lifecycle status. ``platform``
                           is free-text (OQ2); ``status`` is the ``social_account_status``
                           enum. Owner-scoped by ``tenant_id`` + ``user_id``; multiple
                           accounts per ``(user, platform)`` (unique on
                           ``(user_id, platform, external_account_id)``, R4).
- ``social_credentials`` — the envelope-encrypted OAuth tokens (1:1 with an account). The
                           database never stores a usable/plaintext token (C1/C2): only
                           ``ciphertext`` + ``nonce`` + a ``wrapped_dek`` (a per-record data
                           key wrapped by the externally-managed master key) + rotation
                           metadata. ``access_token_expires_at`` is the only non-secret
                           timing field, kept out of the ciphertext to drive refresh.

Schema reference: ``docs/database/schema.md`` §26 (Publishing). Structure created by
migration ``0013_social_accounts``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import publish_status_enum, social_account_status_enum
from app.infrastructure.db.mixins import (
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class SocialAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "social_accounts"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        social_account_status_enum, nullable=False, server_default=text("'connected'")
    )
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "platform",
            "external_account_id",
            name="uq_social_accounts_user_platform_external",
        ),
        Index("ix_social_accounts_user_id", "user_id"),
        Index("ix_social_accounts_tenant_id", "tenant_id"),
    )


class SocialCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "social_credentials"

    social_account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("social_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'AES-256-GCM'")
    )
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("social_account_id", name="uq_social_credentials_social_account_id"),
    )


class PublishJob(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    """Publish-runtime job (α8.6b). Faithful adaptation of ``export_jobs`` (DQ8).

    Direct ownership (``tenant_id`` + ``requested_by_user_id``); an explicit ``project_id``
    (DQ1) powers the project serialisation lock. ``source_media_asset_id`` is the export
    delivery artifact consumed (PUB-1); ``content_package`` is the deterministic metadata
    snapshot (PUB-9). No credential material — the worker fetches an ``AuthorizedContext``
    at run time (PUB-5). Structure created by migration ``0014_publish_jobs``.
    """

    __tablename__ = "publish_jobs"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_export_job_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("export_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_media_asset_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    social_account_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("social_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(publish_status_enum, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    content_package: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    platform_post_id: Mapped[str | None] = mapped_column(Text)
    platform_post_url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # DQ2 idempotency backstop: at most one active-or-fulfilled publish per
        # (source_media_asset_id, social_account_id). failed/canceled excluded (retry OK).
        Index(
            "uq_publish_jobs_source_media_asset_social_account",
            "source_media_asset_id",
            "social_account_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','succeeded')"),
        ),
        Index("ix_publish_jobs_status_scheduled_at", "status", "scheduled_at"),
        Index(
            "ix_publish_jobs_requested_by_user_id_created_at",
            "requested_by_user_id",
            "created_at",
        ),
        Index("ix_publish_jobs_social_account_id", "social_account_id"),
    )


__all__ = ["SocialAccount", "SocialCredential", "PublishJob"]
