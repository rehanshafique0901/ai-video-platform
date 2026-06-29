"""Phase 3 Wave 1.3 — promote ``distributed_locks`` lease sanity to a database
CHECK constraint.

See: docs/decisions/ADR-0032-distributed-locks-lease-check.md

Single coordinated change: add ``chk_distributed_locks_lease_until_after_acquired_at``
enforcing ``lease_until > acquired_at`` at the database level. Strict
greater-than (``>``, not ``>=``) because ``lease_until == acquired_at``
represents a degenerate zero-second lease that the acquisition path should
never compute. Promotes the §37 Q10 invariant verbatim — no bundling with
``lease_until >= heartbeat_at`` or other temporal-anchor invariants (those
remain future-ADR territory per ADR-0032 §Alternatives items 2 and 3).

The Phase 2D reconciliation note in ``schema.md`` §32 (lines 1208-1217)
explicitly deferred this CHECK to a Phase-3 decision; ROADMAP.md line 173
W1.3 plus ADR-0032 are that decision. Pre-upgrade safety SELECT against
the live target must return ``0`` violators before this migration is run
(see ADR §Migration Plan §3 / Acceptance Criteria #11) — adding a CHECK
to a populated table with violators fails immediately, in which case the
production-rollback variant (``ADD CONSTRAINT ... NOT VALID`` followed by
``VALIDATE CONSTRAINT`` in a separate follow-up migration) would be the
required path.

Hand-written rather than via ``alembic revision --autogenerate`` because
autogenerate does not reliably preserve the exact text of CHECK
expressions and would lose the explicit constraint name.

Filename note: the revision string is ``0005_distributed_locks_lease``
(28 chars) rather than the more verbose ``0005_distributed_locks_lease_check``
(34 chars) because Alembic's default ``alembic_version.version_num`` column
is ``VARCHAR(32)``. W1.2 squeaked through at exactly 32 chars; W1.3 with
the longest table name (``distributed_locks`` = 17 chars) needs a shorter
suffix to fit. Dropping ``_check`` is safe because the constraint name
itself (``chk_distributed_locks_lease_until_after_acquired_at``) carries
the CHECK semantic in its prefix and the migration body is literally a
single CHECK addition. A separate future infra PR (``fix/alembic-version-widen``)
will widen the column to ``VARCHAR(255)`` to remove the ceiling globally.

Revision ID: 0005_distributed_locks_lease
Revises: 0004_idempotency_keys_invariants
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_distributed_locks_lease"
down_revision: str | None | Sequence[str] = "0004_idempotency_keys_invariants"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


_TABLE_NAME = "distributed_locks"
_CHECK_NAME = "chk_distributed_locks_lease_until_after_acquired_at"
_CHECK_PREDICATE = "lease_until > acquired_at"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {_TABLE_NAME} "
        f"ADD CONSTRAINT {_CHECK_NAME} "
        f"CHECK ({_CHECK_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {_TABLE_NAME} DROP CONSTRAINT {_CHECK_NAME}"
    )
