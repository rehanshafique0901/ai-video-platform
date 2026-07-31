"""Identity Runtime ORM rows — the creator-authored world (migration ``0017``).

Mirrors ``0017_identity_runtime`` exactly; the migration is the source of truth
and these declarations are the ORM's view of it. Domain equivalents live in
``app/domain/identity_runtime/`` and stay deliberately distinct (ARCHITECTURE.md
§3): the domain owns the caps and the ordering, the database owns per-profile
key uniqueness and the OCC bump.

Not the authentication context — that is ``models/identity.py`` (``users``,
``tenants``, ``sessions``). Different bounded context, no relationship.

Only the root carries ``VersionMixin`` and timestamps: children are written
through the profile and bump *its* version (PF8), so a snapshot taken at any
moment reflects one coherent world.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin, VersionMixin


class IdentityProfile(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    """One creator's world: the aggregate root."""

    __tablename__ = "identity_profiles"

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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    global_style: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pixar'"))
    camera_style: Mapped[str | None] = mapped_column(Text)
    lighting: Mapped[str | None] = mapped_column(Text)
    color_palette: Mapped[str | None] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("uq_identity_profiles_owner_name", "owner_user_id", "name", unique=True),
        Index(
            "ix_identity_profiles_owner_created",
            "owner_user_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class IdentityCharacter(UUIDPrimaryKeyMixin, Base):
    """A person the creator declares exists, carried into every shot unchanged."""

    __tablename__ = "identity_characters"

    profile_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("identity_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    character_key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    age: Mapped[str | None] = mapped_column(Text)
    appearance: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    clothing: Mapped[str | None] = mapped_column(Text)
    accessories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        Index("uq_identity_characters_profile_key", "profile_id", "character_key", unique=True),
    )


class IdentityLocation(UUIDPrimaryKeyMixin, Base):
    """The place the world happens in."""

    __tablename__ = "identity_locations"

    profile_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("identity_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    location_key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    descriptors: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        Index("uq_identity_locations_profile_key", "profile_id", "location_key", unique=True),
    )


class IdentityProp(UUIDPrimaryKeyMixin, Base):
    """A recurring object that must stay consistent across shots."""

    __tablename__ = "identity_props"

    profile_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("identity_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    prop_key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    descriptors: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        Index("uq_identity_props_profile_key", "profile_id", "prop_key", unique=True),
    )
