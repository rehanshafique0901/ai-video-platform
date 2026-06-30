"""Infrastructure cleanup — widen alembic_version.version_num to VARCHAR(255).

See: docs/engineering/RUNBOOK_WAVE.md §6.1 (the VARCHAR(32) ceiling);
CHANGELOG.md v0.3.3-infra entry for full context.

Alembic's bootstrap creates ``alembic_version`` with ``version_num
VARCHAR(32)``. That length was enough for the first five Wave migrations
(the longest, W1.2's ``0004_idempotency_keys_invariants``, fits at exactly
32 characters), but W1.3 hit the ceiling: its natural revision ID
``0005_distributed_locks_lease_check`` (34 chars) failed
``alembic upgrade head`` with ``psycopg.errors.StringDataRightTruncation``
and was renamed in-place to ``0005_distributed_locks_lease`` (28 chars)
to fit. W1.4's natural slug ``0007_usage_records_request_id_unique`` (35
chars) would hit the same wall.

This migration widens the column to ``VARCHAR(255)`` so future Wave
migrations can use descriptive slugs without abbreviation gymnastics.
The 255 cap is the common Alembic-recommended ceiling and is comfortable
headroom for any plausible revision-ID convention.

PostgreSQL's ``ALTER COLUMN ... TYPE VARCHAR(N)`` on a single tiny
system-managed table (``alembic_version`` typically holds one row) is
effectively instantaneous; no maintenance window required.

The migration's own revision ID ``0006_widen_alembic_version_num`` is 30
characters, which fits the EXISTING ``VARCHAR(32)`` limit — so the
migration can apply itself cleanly without the chicken-and-egg problem.
After ``upgrade()`` runs, Alembic inserts ``0006_widen_alembic_version_num``
into the now-widened column (the widen DDL and the row insert happen in
the same transaction).

The ``downgrade()`` returns to ``VARCHAR(32)``. Note: if a later
migration with a >32-char ID is currently in head when downgrade is
invoked, the ALTER will fail with the same truncation error because the
existing row no longer fits the narrower type. That is correct fail-loud
behavior — to actually downgrade past this migration once a long-ID
migration has been applied, the operator must first
``alembic downgrade`` to a revision with a ≤32-char ID before invoking
this migration's downgrade.

Hand-written rather than via ``alembic revision --autogenerate``
because autogenerate does not emit DDL against system tables like
``alembic_version``.

Revision ID: 0006_widen_alembic_version_num
Revises: 0005_distributed_locks_lease
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_widen_alembic_version_num"
down_revision: str | None | Sequence[str] = "0005_distributed_locks_lease"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


def upgrade() -> None:
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)")


def downgrade() -> None:
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(32)")
