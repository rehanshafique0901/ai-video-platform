"""α9.0 — Creator Analytics Foundation (analytics_events idempotency + read index).

See: docs/engineering/PHASE3_ALPHA9_0_PREFLIGHT.md §AN6
     docs/decisions/ADR-0048-analytics-consumer-idempotency.md
     docs/database/schema.md §26 (analytics_events)

Activates the dormant, partitioned ``analytics_events`` table as a **downstream outbox
consumer** (ADR-0042) by adding the persistence-layer half of ADR-0048's DB-enforced
exactly-once contract, plus the owner-scoped read index the analytics summary needs:

    ALTER TABLE analytics_events ADD COLUMN source_event_id uuid NULL;

    CREATE UNIQUE INDEX uq_analytics_events_source_event_id
      ON analytics_events (source_event_id, occurred_at)
     WHERE source_event_id IS NOT NULL;

    CREATE INDEX ix_analytics_events_user_id_occurred_at
      ON analytics_events (user_id, occurred_at)
     WHERE user_id IS NOT NULL;

``source_event_id`` is the outbox ``event.id`` that produced the row — the relay's own
idempotency coordinate. It is **nullable** (a future direct/client analytics ingest path
would leave it NULL) with **no FK** to ``event_outbox`` (the outbox is transient transport
state that may be pruned/parked; the id is a logical dedupe key, not a lifetime coupling).

The unique index is **partial** (``WHERE source_event_id IS NOT NULL``) so any future
non-event rows coexist freely, and — because ``analytics_events`` is ``PARTITION BY RANGE
(occurred_at)`` — it **includes the partition key** ``occurred_at``. PostgreSQL requires a
unique index on a partitioned table to contain every partition-key column; with it included,
the parent-level index is valid and **auto-propagates** to every current and future child
partition (empirically verified against PostgreSQL 17.10 — ADR-0048 §Verification). The
consumer sets ``occurred_at = event.occurred_at`` (deterministic), so a redelivery of the
same event always targets the identical ``(source_event_id, occurred_at)`` pair and is
refused by the index — that is why deterministic ``occurred_at`` is load-bearing.

Additive and safe: a new nullable column + two indexes on a currently **empty** table (no
rows to backfill or conflict). Plain (transactional) DDL — Alembic wraps each migration in a
transaction and the table is empty in every current environment. ``downgrade`` drops both
indexes + the column, so each ci_gate upgrade→downgrade→upgrade roundtrip (stages 5-7) runs
on a clean slate.

Revision ID: 0015_analytics_events_source_event_id
Revises: 0014_publish_jobs
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "0015_analytics_events_source_event_id"
down_revision: str | None | Sequence[str] = "0014_publish_jobs"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


_TABLE = "analytics_events"
_COLUMN = "source_event_id"
_UNIQUE_INDEX = "uq_analytics_events_source_event_id"
_READ_INDEX = "ix_analytics_events_user_id_occurred_at"


def upgrade() -> None:
    """Add nullable ``source_event_id`` + the exactly-once and owner-read indexes."""
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, PgUUID(as_uuid=True), nullable=True),
    )
    # DB-enforced exactly-once (ADR-0048). Includes the partition key ``occurred_at`` so the
    # parent unique index is valid on the partitioned table and auto-propagates to children.
    op.create_index(
        _UNIQUE_INDEX,
        _TABLE,
        [_COLUMN, "occurred_at"],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )
    # Owner-scoped read path for GET /analytics/summary (partial: rows always carry user_id
    # today, but the column is nullable, so the predicate keeps the index tight).
    op.create_index(
        _READ_INDEX,
        _TABLE,
        ["user_id", "occurred_at"],
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop both indexes + the ``source_event_id`` column."""
    op.drop_index(_READ_INDEX, table_name=_TABLE)
    op.drop_index(_UNIQUE_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
