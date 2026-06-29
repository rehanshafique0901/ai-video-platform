"""Mixin contracts.

Mixins are the cheapest way to make the schema consistent across ~50
tables. If a mixin starts emitting different columns over time, all
downstream tables silently drift. These tests pin the public surface of
each mixin so any change requires updating both the mixin and the tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import DateTime, Integer
from sqlalchemy.orm import DeclarativeBase

from app.infrastructure.db.mixins import (
    CreatedAtOnlyMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class _IsolatedBase(DeclarativeBase):
    """A throwaway declarative base so mixin-based models in tests do not
    leak into ``app.infrastructure.db.metadata`` and pollute the production
    metadata used by the rest of the suite."""


class _ProbeTable(
    UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, VersionMixin, _IsolatedBase
):
    __tablename__ = "_probe_mutable"


class _ImmutableProbe(UUIDPrimaryKeyMixin, CreatedAtOnlyMixin, _IsolatedBase):
    __tablename__ = "_probe_immutable"


@pytest.mark.unit
def test_uuid_primary_key_mixin_exposes_id_as_uuid_column() -> None:
    column = _ProbeTable.__table__.c.id
    assert column.primary_key is True
    assert column.nullable is False
    # The mixin module uses ``from __future__ import annotations`` so
    # ``__annotations__`` stores the textual form. We assert against the
    # string the mixin declares, which is stable regardless of how
    # SQLAlchemy generics evolve.
    annotation = UUIDPrimaryKeyMixin.__annotations__.get("id")
    assert annotation == "Mapped[UUID]", annotation
    # Belt-and-braces: confirm the dialect type stored on the column is
    # the Postgres-flavoured UUID. We compare on the SQL type name so a
    # SQLAlchemy version bump that re-wraps the type does not falsely
    # break the test.
    assert "UUID" in column.type.__class__.__name__


@pytest.mark.unit
def test_timestamp_mixin_has_created_and_updated_at() -> None:
    cols = {c.name: c for c in _ProbeTable.__table__.columns}
    for required in ("created_at", "updated_at"):
        assert required in cols, f"TimestampMixin must expose {required}"
        col = cols[required]
        assert col.nullable is False
        assert isinstance(col.type, DateTime)
        assert col.type.timezone, f"{required} must be timezone-aware (timestamptz)"


@pytest.mark.unit
def test_soft_delete_mixin_is_nullable() -> None:
    deleted_at = _ProbeTable.__table__.c.deleted_at
    assert deleted_at.nullable, "SoftDeleteMixin.deleted_at must be nullable"
    assert isinstance(deleted_at.type, DateTime)
    assert deleted_at.type.timezone, "deleted_at must be timestamptz"


@pytest.mark.unit
def test_version_mixin_is_monotonic_int() -> None:
    version = _ProbeTable.__table__.c.version
    assert version.nullable is False
    assert isinstance(version.type, Integer)
    # Default behavior: server-side default of "1". We use the textual
    # comparison because SQLAlchemy wraps it in a TextClause.
    assert version.server_default is not None
    assert "1" in str(version.server_default.arg), version.server_default.arg


@pytest.mark.unit
def test_created_at_only_mixin_is_strictly_minimal() -> None:
    """Immutable tables must NOT inherit updated_at/deleted_at/version."""

    cols = {c.name for c in _ImmutableProbe.__table__.columns}
    assert "created_at" in cols
    forbidden = cols & {"updated_at", "deleted_at", "version"}
    assert (
        not forbidden
    ), f"CreatedAtOnlyMixin must not introduce mutation columns; got {sorted(forbidden)}"


@pytest.mark.unit
def test_default_python_factory_on_uuid_pk_is_uuid4() -> None:
    """The Python-side default on the PK must be ``uuid4`` (the callable),
    not a string. SQLAlchemy fires column defaults at INSERT time, not at
    ``Model()`` instantiation, so we inspect the column descriptor
    directly rather than constructing an instance.
    """

    column = _ProbeTable.__table__.c.id
    assert column.default is not None, "UUIDPrimaryKeyMixin must declare a Python-side default"
    factory = column.default.arg
    assert callable(factory), f"expected a callable; got {factory!r}"
    # Compare by qualified name rather than identity: test discovery can
    # re-import ``uuid``, producing distinct-but-equivalent function
    # objects that break ``is``-identity even though both refer to the
    # same callable.
    # SQLAlchemy may wrap a 0-arg factory to be "context-aware" (taking
    # a hidden ExecutionContext), so we cannot reliably invoke the
    # wrapped callable here without faking a context. Identity is
    # verified by module + qualname instead.
    assert (
        factory.__module__ == "uuid" and factory.__qualname__ == "uuid4"
    ), f"expected uuid.uuid4; got {factory.__module__}.{factory.__qualname__}"


@pytest.mark.unit
def test_python_default_for_version_is_one() -> None:
    """``VersionMixin`` must declare a Python-side default of literal ``1``.

    Same caveat as above: defaults are applied on INSERT, not on
    instantiation. We assert against the column descriptor.
    """

    column = _ProbeTable.__table__.c.version
    assert column.default is not None, "VersionMixin must declare a Python-side default"
    assert column.default.arg == 1, f"expected default of 1; got {column.default.arg!r}"


@pytest.mark.unit
def test_python_defaults_do_not_fire_for_server_timestamps() -> None:
    """``created_at`` / ``updated_at`` rely on the DB ``server_default`` and
    must NOT be populated client-side; otherwise tests can mask clock drift."""

    instance = _ProbeTable()
    # Attribute may be unset; getattr returns None when unloaded.
    assert getattr(instance, "created_at", None) is None
    assert getattr(instance, "updated_at", None) is None
    # And deleted_at is nullable by definition.
    assert getattr(instance, "deleted_at", None) is None


@pytest.mark.unit
def test_uuid_pk_server_default_is_gen_random_uuid() -> None:
    column = _ProbeTable.__table__.c.id
    assert column.server_default is not None
    assert "gen_random_uuid" in str(column.server_default.arg)
