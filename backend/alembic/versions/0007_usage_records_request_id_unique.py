"""Phase 3 Wave 1.4 — promote ``usage_records`` per-partition ``request_id``
uniqueness to a database constraint.

See: docs/decisions/ADR-0033-usage-records-request-id-unique.md
     docs/engineering/RUNBOOK_WAVE.md (operational steps; first migration-coupled
     ADR to reference the runbook in place of inlining them).

Single coordinated change: create one partial-unique index per child partition
of ``usage_records`` named ``uq_<child>_request_id`` enforcing the predicate
``(request_id) WHERE request_id IS NOT NULL``. Partial because ``request_id``
is nullable (system-initiated calls without a vendor id leave it NULL and
must be allowed to coexist). Per-partition because PostgreSQL forbids unique
indexes on a partitioned parent unless the unique key includes every
partition-key column — for ``usage_records`` (``PARTITION BY RANGE
(occurred_at)``) that would require ``(request_id, occurred_at)``, whose
semantic is too weak to be worth shipping. The per-child mechanic is
PostgreSQL's standard and correct pattern for this case; it is not a
workaround for a limitation.

Resolves ``docs/database/schema.md`` §37 Q6 (the W1.4-specific framing,
"Should ``usage_records`` add a per-partition partial-unique ``(request_id)``
index, or rely on ``idempotency_keys``?"). Broader provider-scoped
idempotency semantics remain documented elsewhere in the architecture
(``schema.md`` §18 Step-A draft, §31 line 1175, ``API_CONTRACT.md`` line 233,
``ARCHITECTURE.md`` §8k.1); W1.4 implements the scope reflected in the
Phase 3 planning artifacts without attempting to reconcile that broader
architectural question. See ADR-0033 §Future Considerations.

Pre-upgrade safety SELECT against the live target must return zero rows
before this migration runs (see ADR-0033 §Migration Plan / Acceptance
Criterion 13):

    SELECT request_id, count(*)
    FROM usage_records
    WHERE request_id IS NOT NULL
    GROUP BY request_id
    HAVING count(*) > 1;

The table is empty in every current environment; this is expected to be
trivially zero. A non-zero result would force the production-rollback
variant (``ADD CONSTRAINT ... NOT VALID`` + later ``VALIDATE CONSTRAINT``
in a separate follow-up migration) — out of scope here.

Hand-written rather than via ``alembic revision --autogenerate`` because
autogenerate cannot express per-child partition-level DDL, would not
preserve the ``WHERE request_id IS NOT NULL`` partial predicate, and has
no concept of the per-partition propagation loop.

The ORM does NOT declare these indexes (see ``backend/app/infrastructure/
db/models/usage.py`` inline comment and ADR-0033 §Implementation Notes —
ORM declaration intentionally absent). The CI-visibility addition lives in
``backend/scripts/validate_schema.py::check_usage_records_per_partition_
unique_indexes``.

Future-partition contract (per ADR-0033 §Implementation Notes — Future-
partition contract): PostgreSQL native inheritance does NOT auto-propagate
per-child unique indexes to new partitions. Whatever creates new
``usage_records`` partitions in the future (currently only the baseline
migration's ``_create_initial_partitions`` block; future: the rolling-window
helper referenced at ``schema.md`` §18 line 671) must add the matching
``uq_<child>_request_id`` index when the partition is created. The
validator check enforces this at CI time.

Revision ID: 0007_usage_records_request_id_unique
Revises: 0006_widen_alembic_version_num
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_usage_records_request_id_unique"
down_revision: str | None | Sequence[str] = "0006_widen_alembic_version_num"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


_PARENT_TABLE = "usage_records"
_INDEX_NAME_TEMPLATE = "uq_{child}_request_id"
_PREDICATE = "request_id IS NOT NULL"


def upgrade() -> None:
    """Create one partial-unique index per child partition of usage_records.

    Iterates ``pg_inherits`` for all current children of ``usage_records``
    (26 monthly + 1 DEFAULT today) and creates::

        CREATE UNIQUE INDEX uq_<child>_request_id
          ON <child> (request_id)
         WHERE request_id IS NOT NULL;

    Idempotent via ``IF NOT EXISTS`` so the migration can be re-run cleanly
    against an already-upgraded database (CI gate stage 7 round-trip).
    """
    op.execute(
        """
        DO $$
        DECLARE
            child_name text;
            index_name text;
        BEGIN
            FOR child_name IN
                SELECT c.relname
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                JOIN pg_class p ON p.oid = i.inhparent
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE p.relname = 'usage_records'
                  AND n.nspname = 'public'
                ORDER BY c.relname
            LOOP
                index_name := 'uq_' || child_name || '_request_id';
                EXECUTE format(
                    'CREATE UNIQUE INDEX IF NOT EXISTS %I '
                    'ON %I (request_id) '
                    'WHERE request_id IS NOT NULL',
                    index_name, child_name
                );
            END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop the per-partition unique indexes added by ``upgrade()``.

    Mirror loop with ``DROP INDEX IF EXISTS``. The parent's non-unique
    propagating ``ix_usage_records_request_id`` index is **not** affected
    — it was created by the baseline migration and is owned by that
    migration's downgrade path.
    """
    op.execute(
        """
        DO $$
        DECLARE
            child_name text;
            index_name text;
        BEGIN
            FOR child_name IN
                SELECT c.relname
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                JOIN pg_class p ON p.oid = i.inhparent
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE p.relname = 'usage_records'
                  AND n.nspname = 'public'
                ORDER BY c.relname
            LOOP
                index_name := 'uq_' || child_name || '_request_id';
                EXECUTE format('DROP INDEX IF EXISTS %I', index_name);
            END LOOP;
        END $$;
        """
    )
