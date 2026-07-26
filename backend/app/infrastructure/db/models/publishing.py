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
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import social_account_status_enum
from app.infrastructure.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


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


__all__ = ["SocialAccount", "SocialCredential"]
