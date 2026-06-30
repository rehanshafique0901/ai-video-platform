"""Automated schema validation for Phase 2 Step B.

Connects to the configured PostgreSQL database (assumed to already be at
``alembic upgrade head`` — call ``run_validation.py`` to orchestrate the full
upgrade/downgrade/re-upgrade cycle) and validates:

  1. Every table declared in ``app.infrastructure.db.metadata`` exists.
  2. Every documented index from ``docs/database/INDEX_STRATEGY.md`` exists
     (best-effort; we use the expected index list embedded below since
     INDEX_STRATEGY.md is prose, not machine-readable).
  3. Every foreign key declared in the ORM exists with the correct
     ``on_delete`` action.
  4. Every unique constraint declared in the ORM exists.
  5. Partitioned parents are partitioned and have at least one child.
  6. Immutable tables carry the expected ``reject_mutation`` BEFORE
     UPDATE/DELETE trigger.
  7. The ``vector`` column type appears only on the approved tables
     (``library_assets.embedding`` and ``agent_memory.embedding``).

The script writes a JSON summary to ``schema_validation_report.json`` and
prints a human-readable summary to stdout. Exit code is non-zero if any
required check failed.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

# Make the backend package importable when run as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _load_env  # noqa: E402

from app.infrastructure.db import models  # noqa: F401,E402
from app.infrastructure.db.base import metadata  # noqa: E402

DATABASE_URL = _load_env.load()


def _redact(url: str) -> str:
    return re.sub(r"//([^:]+):[^@]+@", r"//\1:***@", url)


# ---------------------------------------------------------------------------
# Expected entities (kept in sync with docs/database/*.md)
# ---------------------------------------------------------------------------
EXPECTED_PARTITIONED = {"usage_records", "analytics_events", "event_log", "audit_log"}

EXPECTED_IMMUTABLE = {
    "project_versions",
    "ai_model_pricing",
    "workflow_checkpoints",
    "usage_records",
    "credit_ledger",
    "analytics_events",
    "event_log",
    "audit_log",
}

EXPECTED_PGVECTOR_COLUMNS = {
    ("library_assets", "embedding"),
    ("agent_memory", "embedding"),
}

EXPECTED_EXTENSIONS = {"pgcrypto", "citext", "pg_trgm", "vector", "btree_gin"}

# Indexes that are declared imperatively in the baseline migration (not via
# SQLAlchemy Index objects) and therefore wouldn't be picked up by an inspect
# loop over the ORM metadata. Listed here so the validator catches drift.
EXTRA_EXPECTED_INDEXES = {
    "ix_ai_models_capabilities_gin",
    "ix_library_assets_tags_gin",
    "ix_library_assets_embedding_hnsw",
    "ix_analytics_events_properties_gin",
    "ix_agent_memory_embedding_hnsw",
}


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    passed: bool
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "details": self.details}


@dataclass
class ValidationReport:
    database_url: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "database_url": self.database_url,
            "all_passed": self.all_passed,
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_extensions(engine: Engine) -> CheckResult:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT extname FROM pg_extension")).all()
    installed = {r[0] for r in rows}
    missing = EXPECTED_EXTENSIONS - installed
    return CheckResult(
        name="Required PostgreSQL extensions",
        passed=not missing,
        details=(
            [f"Missing extensions: {sorted(missing)}"]
            if missing
            else [f"Installed: {sorted(installed & EXPECTED_EXTENSIONS)}"]
        ),
    )


ALLOWED_EXTRA_TABLES = {"alembic_version"}

# ---------------------------------------------------------------------------
# Bulk pg_catalog snapshot
#
# The SQLAlchemy inspector issues one round-trip per table for
# get_foreign_keys / get_unique_constraints / get_indexes. With ~52 base tables
# plus ~108 partition children, that's >400 round-trips and easily exhausts
# Supabase's 2-minute statement_timeout (or just takes 4 minutes over a
# cross-region pooler). We replace those calls with three bulk pg_catalog
# queries that load everything in a single round-trip each, pre-filter
# partition children at the SQL level, and let every check_* function consume
# the cached snapshot.
# ---------------------------------------------------------------------------


@dataclass
class FKRecord:
    table: str
    column: str
    ref_table: str
    ref_column: str
    on_delete: str  # "NO ACTION" | "RESTRICT" | "CASCADE" | "SET NULL" | "SET DEFAULT"
    name: str


@dataclass
class IndexRecord:
    table: str
    name: str
    is_unique: bool


@dataclass
class CatalogSnapshot:
    base_tables: set[str]  # public, not-inherited, excludes alembic_version
    all_tables: set[str]  # public, including partition children
    partition_children: set[str]
    partitioned_parents: set[str]
    partition_children_by_parent: dict[str, int]
    fks: list[FKRecord]  # excludes partition children at source
    fks_by_table: dict[str, list[FKRecord]]
    indexes: list[IndexRecord]  # excludes partition children at source
    index_names: set[str]
    unique_index_names: set[str]


_PG_DELACTION = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def load_snapshot(engine: Engine) -> CatalogSnapshot:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                """
                SELECT
                    c.relname AS name,
                    c.relkind AS kind,
                    EXISTS(SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid)
                        AS is_child
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                """
            )
        ).all()
        all_tables: set[str] = {r[0] for r in rows}
        partition_children = {r[0] for r in rows if r[2]}
        partitioned_parents = {r[0] for r in rows if r[1] == "p"}
        base_tables = all_tables - partition_children - ALLOWED_EXTRA_TABLES

        children_by_parent = {
            r[0]: r[1]
            for r in conn.execute(
                sa.text(
                    """
                    SELECT parent.relname, COUNT(*)
                    FROM pg_inherits i
                    JOIN pg_class parent ON parent.oid = i.inhparent
                    JOIN pg_namespace n ON n.oid = parent.relnamespace
                    WHERE n.nspname = 'public'
                    GROUP BY parent.relname
                    """
                )
            ).all()
        }

        # Foreign keys (single round-trip). conkey/confkey are smallint[] —
        # unnest in parallel to get column-by-column tuples but our schema only
        # has single-column FKs, so taking position 1 suffices.
        fk_rows = conn.execute(
            sa.text(
                """
                SELECT
                    con.conname        AS name,
                    src.relname        AS src,
                    src_col.attname    AS src_col,
                    dst.relname        AS dst,
                    dst_col.attname    AS dst_col,
                    con.confdeltype    AS confdeltype
                FROM pg_constraint con
                JOIN pg_class src ON src.oid = con.conrelid
                JOIN pg_namespace srcns ON srcns.oid = src.relnamespace
                JOIN pg_class dst ON dst.oid = con.confrelid
                JOIN pg_attribute src_col
                  ON src_col.attrelid = con.conrelid
                 AND src_col.attnum   = con.conkey[1]
                JOIN pg_attribute dst_col
                  ON dst_col.attrelid = con.confrelid
                 AND dst_col.attnum   = con.confkey[1]
                WHERE con.contype = 'f'
                  AND srcns.nspname = 'public'
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_inherits i WHERE i.inhrelid = src.oid
                  )
                """
            )
        ).all()
        fks = [
            FKRecord(
                table=r[1],
                column=r[2],
                ref_table=r[3],
                ref_column=r[4],
                on_delete=_PG_DELACTION.get(r[5], "NO ACTION"),
                name=r[0],
            )
            for r in fk_rows
        ]
        fks_by_table: dict[str, list[FKRecord]] = {}
        for f in fks:
            fks_by_table.setdefault(f.table, []).append(f)

        idx_rows = conn.execute(
            sa.text(
                """
                SELECT
                    t.relname     AS table_name,
                    ic.relname    AS index_name,
                    ix.indisunique AS is_unique
                FROM pg_index ix
                JOIN pg_class ic ON ic.oid = ix.indexrelid
                JOIN pg_class t  ON t.oid  = ix.indrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = 'public'
                  AND NOT ix.indisprimary
                  AND t.relkind IN ('r', 'p')
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_inherits i WHERE i.inhrelid = t.oid
                  )
                """
            )
        ).all()
        indexes = [IndexRecord(table=r[0], name=r[1], is_unique=r[2]) for r in idx_rows]
        index_names = {i.name for i in indexes}
        unique_index_names = {i.name for i in indexes if i.is_unique}

    return CatalogSnapshot(
        base_tables=base_tables,
        all_tables=all_tables,
        partition_children=partition_children,
        partitioned_parents=partitioned_parents,
        partition_children_by_parent=children_by_parent,
        fks=fks,
        fks_by_table=fks_by_table,
        indexes=indexes,
        index_names=index_names,
        unique_index_names=unique_index_names,
    )


def check_tables(snap: CatalogSnapshot) -> CheckResult:
    expected = {t.name for t in metadata.sorted_tables}
    missing = expected - snap.base_tables
    extra = snap.base_tables - expected
    details = []
    if missing:
        details.append(f"Missing tables: {sorted(missing)}")
    if extra:
        details.append(f"Extra tables (not in ORM metadata): {sorted(extra)}")
    if not missing and not extra:
        details.append(
            f"All {len(expected)} ORM-declared tables present "
            f"(Alembic-managed extras ignored: {sorted(ALLOWED_EXTRA_TABLES)})."
        )
    return CheckResult(
        name="Tables match ORM metadata",
        passed=not missing and not extra,
        details=details,
    )


def check_partitions(snap: CatalogSnapshot) -> CheckResult:
    details: list[str] = []
    passed = True
    for parent in sorted(EXPECTED_PARTITIONED):
        if parent not in snap.partitioned_parents:
            passed = False
            details.append(f"{parent}: NOT partitioned")
            continue
        n = snap.partition_children_by_parent.get(parent, 0)
        if n == 0:
            passed = False
            details.append(f"{parent}: partitioned but has no children")
        else:
            details.append(f"{parent}: partitioned, {n} children")
    return CheckResult(name="Partitioned tables", passed=passed, details=details)


def check_foreign_keys(snap: CatalogSnapshot) -> CheckResult:
    missing: list[str] = []
    on_delete_mismatch: list[str] = []
    for table in metadata.sorted_tables:
        for fk in table.foreign_keys:
            ref_table = fk.column.table.name
            ref_column = fk.column.name
            local_column = fk.parent.name
            expected_ondelete = (fk.ondelete or "").upper() or "NO ACTION"
            matched = None
            for af in snap.fks_by_table.get(table.name, []):
                if (
                    af.ref_table == ref_table
                    and af.column == local_column
                    and af.ref_column == ref_column
                ):
                    matched = af
                    break
            if matched is None:
                missing.append(f"{table.name}.{local_column} -> {ref_table}.{ref_column}")
                continue
            if matched.on_delete != expected_ondelete:
                on_delete_mismatch.append(
                    f"{table.name}.{local_column} -> {ref_table}.{ref_column} "
                    f"expected ON DELETE {expected_ondelete}, got {matched.on_delete}"
                )
    passed = not missing and not on_delete_mismatch
    details: list[str] = []
    if missing:
        details.append(f"Missing FKs: {missing}")
    if on_delete_mismatch:
        details.append(f"ON DELETE mismatch: {on_delete_mismatch}")
    if passed:
        details.append(
            f"All {sum(1 for t in metadata.sorted_tables for _ in t.foreign_keys)} "
            "ORM-declared foreign keys present with matching ON DELETE."
        )
    return CheckResult(name="Foreign keys", passed=passed, details=details)


def check_unique_constraints(snap: CatalogSnapshot) -> CheckResult:
    missing: list[str] = []
    for table in metadata.sorted_tables:
        for constraint in table.constraints:
            if (
                isinstance(constraint, sa.UniqueConstraint)
                and constraint.name
                and constraint.name not in snap.unique_index_names
            ):
                missing.append(f"{table.name}.{constraint.name}")
        for index in table.indexes:
            if index.unique and index.name and index.name not in snap.unique_index_names:
                missing.append(f"{table.name}.{index.name}")
    passed = not missing
    return CheckResult(
        name="Unique constraints / unique indexes",
        passed=passed,
        details=(missing or ["All ORM-declared unique constraints present."]),
    )


def check_indexes(snap: CatalogSnapshot) -> CheckResult:
    expected_names: set[str] = set(EXTRA_EXPECTED_INDEXES)
    for table in metadata.sorted_tables:
        for idx in table.indexes:
            if idx.name:
                expected_names.add(idx.name)

    missing = sorted(expected_names - snap.index_names)
    return CheckResult(
        name="Documented indexes present",
        passed=not missing,
        details=(
            [f"Missing indexes: {missing}"]
            if missing
            else [f"All {len(expected_names)} expected indexes present."]
        ),
    )


def check_immutable_triggers(engine: Engine) -> CheckResult:
    sql = sa.text(
        """
        SELECT c.relname AS table_name, t.tgname AS trigger_name
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE t.tgname LIKE 'tg_%_bud_reject_mutation'
          AND NOT t.tgisinternal
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).all()
    actual = {r[0] for r in rows}
    missing = EXPECTED_IMMUTABLE - actual
    return CheckResult(
        name="Immutable tables protected by reject_mutation trigger",
        passed=not missing,
        details=(
            [f"Missing immutability triggers on: {sorted(missing)}"]
            if missing
            else [f"All {len(EXPECTED_IMMUTABLE)} immutable tables protected."]
        ),
    )


def check_pgvector_columns(engine: Engine) -> CheckResult:
    sql = sa.text(
        """
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        WHERE c.udt_name = 'vector'
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).all()
    actual = {(r[0], r[1]) for r in rows}
    extra = actual - EXPECTED_PGVECTOR_COLUMNS
    missing = EXPECTED_PGVECTOR_COLUMNS - actual
    passed = not extra and not missing
    details: list[str] = []
    if missing:
        details.append(f"Missing expected vector columns: {sorted(missing)}")
    if extra:
        details.append(f"Unexpected vector columns: {sorted(extra)}")
    if passed:
        details.append(f"Vector columns appear only on approved tables: {sorted(actual)}")
    return CheckResult(
        name="pgvector usage limited to approved tables", passed=passed, details=details
    )


def check_credit_ledger_balance_trigger(engine: Engine) -> CheckResult:
    sql = sa.text(
        """
        SELECT 1 FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE c.relname = 'credit_ledger'
          AND t.tgname = 'tg_credit_ledger_bi_enforce_balance'
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql).scalar()
    return CheckResult(
        name="credit_ledger balance trigger present",
        passed=bool(row),
        details=(["OK"] if row else ["Trigger missing"]),
    )


def check_usage_records_per_partition_unique_indexes(engine: Engine) -> CheckResult:
    """Phase 3 W1.4 (ADR-0033): per-partition partial-unique (request_id) indexes.

    Asserts that every child partition of ``usage_records`` carries an
    index named ``uq_<child>_request_id`` with ``indisunique = true`` and
    the expected ``WHERE (request_id IS NOT NULL)`` partial predicate.

    Why this is a standalone check rather than a row in the bulk index
    snapshot: ``load_snapshot`` deliberately excludes partition children
    from its bulk index query (see the ``NOT EXISTS (SELECT 1 FROM
    pg_inherits ...)`` clause in the indexes CTE) to avoid hundreds of
    per-child round-trips against Supabase. For the 99% case (indexes
    declared at parent level and propagated by PostgreSQL native
    inheritance) parent-only visibility is sufficient. The W1.4
    per-child unique indexes are the 1% case — they exist only on
    children by PostgreSQL design (the partition-key rule forbids
    declaring them at parent level for ``(request_id)`` because it
    omits the ``occurred_at`` partition key), and would be invisible
    to ``check_indexes`` without this targeted scan.

    This check is a CI-visibility addition compensating for the
    bulk-snapshot performance optimisation; it is not a workaround for
    a PostgreSQL limitation, and it is not a substitute for ORM
    declaration (which is impossible by PostgreSQL design — see
    ADR-0033 §Implementation Notes).
    """
    expected_index_pattern = "uq_<child>_request_id"
    expected_predicate = "WHERE (request_id IS NOT NULL)"

    with engine.connect() as conn:
        children = [
            row[0]
            for row in conn.execute(
                sa.text(
                    """
                    SELECT c.relname
                    FROM pg_inherits i
                    JOIN pg_class c ON c.oid = i.inhrelid
                    JOIN pg_class p ON p.oid = i.inhparent
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE p.relname = 'usage_records'
                      AND n.nspname = 'public'
                    ORDER BY c.relname
                    """
                )
            ).all()
        ]
        present = {
            row[0]: row[1]
            for row in conn.execute(
                sa.text(
                    """
                    SELECT t.relname AS child, pg_get_indexdef(ix.indexrelid) AS def
                    FROM pg_index ix
                    JOIN pg_class ic ON ic.oid = ix.indexrelid
                    JOIN pg_class t  ON t.oid  = ix.indrelid
                    JOIN pg_inherits i ON i.inhrelid = t.oid
                    JOIN pg_class p ON p.oid = i.inhparent
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE p.relname = 'usage_records'
                      AND n.nspname = 'public'
                      AND ix.indisunique = true
                      AND ic.relname = 'uq_' || t.relname || '_request_id'
                    """
                )
            ).all()
        }

    missing = [c for c in children if c not in present]
    bad_predicate = [c for c, defn in present.items() if "request_id IS NOT NULL" not in defn]

    details: list[str]
    if not children:
        # Defensive: if there are no children, the table is unpartitioned or
        # the migration that created partitions has not run. Surface this as
        # a failure rather than silently passing on an empty set.
        details = [
            "FAIL: no usage_records partition children found "
            "(baseline migration 0001 not applied?)"
        ]
        passed = False
    elif missing:
        details = [
            f"Missing per-partition unique indexes on {len(missing)} of "
            f"{len(children)} partition(s): {missing}",
            f"Expected name pattern: {expected_index_pattern}",
            f"Expected predicate:    {expected_predicate}",
        ]
        passed = False
    elif bad_predicate:
        details = [
            f"Indexes present but wrong predicate on: {bad_predicate}",
            f"Expected predicate: {expected_predicate}",
        ]
        passed = False
    else:
        details = [
            f"OK: {len(children)}/{len(children)} usage_records partition(s) "
            f"carry uq_<child>_request_id with partial predicate "
            f"{expected_predicate}",
        ]
        passed = True

    return CheckResult(
        name="usage_records per-partition (request_id) unique indexes",
        passed=passed,
        details=details,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def run_all_checks(engine: Engine) -> ValidationReport:
    report = ValidationReport(database_url=_redact(str(engine.url)))
    # Engine-only checks (single bulk query each)
    report.checks.append(check_extensions(engine))

    # Bulk snapshot for all catalog-driven checks (one query each, vs. ~400
    # per-table inspector round-trips on the previous implementation).
    snap = load_snapshot(engine)
    report.checks.append(check_tables(snap))
    report.checks.append(check_partitions(snap))
    report.checks.append(check_foreign_keys(snap))
    report.checks.append(check_unique_constraints(snap))
    report.checks.append(check_indexes(snap))

    # Engine-only checks (single query each)
    report.checks.append(check_immutable_triggers(engine))
    report.checks.append(check_pgvector_columns(engine))
    report.checks.append(check_credit_ledger_balance_trigger(engine))
    # W1.4 (ADR-0033): per-child unique indexes that load_snapshot's bulk
    # query elides for performance. See the check's docstring for rationale.
    report.checks.append(check_usage_records_per_partition_unique_indexes(engine))
    return report


def main(argv: list[str]) -> int:
    out_path = Path(argv[1]) if len(argv) > 1 else Path("schema_validation_report.json")
    engine = sa.create_engine(DATABASE_URL, future=True)
    report = run_all_checks(engine)

    print(f"\nSchema validation against {report.database_url}\n")
    for c in report.checks:
        icon = "[ OK ]" if c.passed else "[FAIL]"
        print(f"{icon} {c.name}")
        for d in c.details:
            print(f"        {d}")
    print()

    out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(f"Wrote {out_path.resolve()}")
    return 0 if report.all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
