"""Static-only assertions over ``Base.metadata``.

These tests run without a database and verify that the ORM declares
exactly the surface the schema design guarantees:

* every table that lives in a partitioned cluster is range-partitioned
  in the ORM (``postgresql_partition_by`` set);
* every foreign key declares an explicit ``ON DELETE`` action — silent
  ``NO ACTION`` cascades have caused production incidents in past
  projects and are not allowed here;
* the immutable-table contract (no ``updated_at`` / ``deleted_at``
  columns) holds for the eight append-only tables.

If the live-DB validator passes but these unit tests fail, the live
database has been edited out of band; if these pass but the live
validator fails, the migration is incomplete. Either signal is useful.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import app.infrastructure.db.models  # noqa: F401 — trigger model registration
from app.infrastructure.db import metadata

# -- Reference sets sourced from docs/database/schema.md ---------------------

PARTITIONED_PARENTS: frozenset[str] = frozenset(
    {"usage_records", "analytics_events", "event_log", "audit_log"}
)

IMMUTABLE_TABLES: frozenset[str] = frozenset(
    {
        "project_versions",
        "ai_model_pricing",
        "workflow_checkpoints",
        "usage_records",
        "credit_ledger",
        "analytics_events",
        "event_log",
        "audit_log",
    }
)


@pytest.mark.unit
def test_partitioned_parents_declare_range_partition_by() -> None:
    """Every partitioned parent must declare ``postgresql_partition_by`` in the ORM.

    The migration only re-creates the partition children on
    ``alembic upgrade``. If the ORM table is *not* partitioned but the
    migration creates partitions anyway, the metadata-vs-migration
    contract is broken.
    """

    for name in PARTITIONED_PARENTS:
        table = metadata.tables.get(name)
        assert table is not None, f"partitioned parent {name!r} is missing from metadata"
        kwargs = table.dialect_kwargs
        partition_clause = kwargs.get("postgresql_partition_by")
        assert partition_clause, (
            f"{name!r} is declared partitioned in schema.md but the ORM "
            f"table did not set `postgresql_partition_by`. Add the "
            f"`__table_args__ = {{'postgresql_partition_by': 'RANGE (created_at)'}}` "
            f"entry."
        )
        assert (
            "RANGE" in partition_clause.upper()
        ), f"{name!r} is expected to use RANGE partitioning; got {partition_clause!r}"


@pytest.mark.unit
def test_every_foreign_key_declares_on_delete() -> None:
    """Silent ``NO ACTION`` FKs are forbidden by ``schema.md`` §1.

    Each ``ForeignKey`` (or ``ForeignKeyConstraint``) must declare an
    explicit ``ondelete`` argument. The check tolerates ``ondelete=None``
    only for self-referential FKs whose business rule is "leave the row
    alone" — currently zero such FKs exist, so any None is a defect.
    """

    offenders: list[str] = []
    for table in metadata.sorted_tables:
        for fk in table.foreign_keys:
            if not fk.ondelete:
                offenders.append(
                    f"{table.name}.{fk.parent.name} -> " f"{fk.column.table.name}.{fk.column.name}"
                )
    assert (
        not offenders
    ), "the following foreign keys do not declare ON DELETE explicitly: " + ", ".join(offenders)


@pytest.mark.unit
def test_immutable_tables_have_no_updated_at_or_deleted_at() -> None:
    """Append-only tables must not carry mutation columns.

    ``updated_at`` and ``deleted_at`` only make sense for soft-deletable
    rows; their presence on an immutable table is either a copy-paste
    error or a silent contract violation. The matching ``reject_mutation``
    trigger is verified by the live validator, but the *schema* side of
    the contract is enforced here.
    """

    for name in IMMUTABLE_TABLES:
        table = metadata.tables.get(name)
        assert table is not None, f"immutable table {name!r} missing from metadata"
        columns = {c.name for c in table.columns}
        forbidden = columns & {"updated_at", "deleted_at"}
        assert not forbidden, (
            f"immutable table {name!r} declares mutation columns {sorted(forbidden)}; "
            f"either it is not really immutable, or the columns must be removed."
        )


@pytest.mark.unit
def test_pgvector_columns_are_scoped() -> None:
    """``vector`` columns may only appear on the two approved tables.

    A rogue ``vector(1536)`` column anywhere else is almost certainly an
    accidental import; embeddings should live on the two designated
    tables documented in ``schema.md``.
    """

    approved = {("agent_memory", "embedding"), ("library_assets", "embedding")}
    actual: set[tuple[str, str]] = set()
    # The pgvector SQLAlchemy adapter exposes its dialect type as the
    # all-caps `VECTOR` (mirroring the SQL type name). We match it
    # case-insensitively so a future rename (e.g. `Vector`) doesn't
    # silently break this contract.
    for table in metadata.sorted_tables:
        for column in table.columns:
            type_name = column.type.__class__.__name__
            if type_name.lower() == "vector":
                actual.add((table.name, column.name))
    assert actual == approved, (
        f"pgvector usage drift: expected {sorted(approved)}, got {sorted(actual)}. "
        f"Add a new ADR before introducing additional embedding columns."
    )


@pytest.mark.unit
def test_metadata_naming_convention_present() -> None:
    """Naming convention drives constraint names; missing it explodes downstream.

    SQLAlchemy uses ``MetaData.naming_convention`` to derive index, FK,
    unique, and check constraint names. If it's empty, auto-generated
    names will not match ``INDEX_STRATEGY.md`` and Alembic autogenerate
    will produce churn-y diffs.
    """

    nc = metadata.naming_convention
    expected_keys = {"ix", "uq", "ck", "fk", "pk"}
    missing = expected_keys - set(nc.keys())
    assert not missing, (
        f"metadata.naming_convention is missing keys: {sorted(missing)}; "
        f"see app/infrastructure/db/base.py"
    )


@pytest.mark.unit
def test_no_orm_table_uses_naive_datetime() -> None:
    """All timestamps must be ``timestamptz``. Naive datetimes are forbidden by ADR-0007."""

    offenders: list[str] = []
    for table in metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, sa.DateTime) and not column.type.timezone:
                offenders.append(f"{table.name}.{column.name}")
    assert not offenders, "the following datetime columns are NOT timezone-aware: " + ", ".join(
        offenders
    )
