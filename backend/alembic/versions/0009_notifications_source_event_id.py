"""Phase 3 Slice α8.5b.3 — notification exactly-once projection key.

See: docs/engineering/PHASE3_ALPHA8_5b3_PREFLIGHT.md §4 / §6
     docs/database/schema.md §25 (notifications)

Adds the persistence-layer half of W8.5b.7 ("a notification is projected exactly
once per recipient per source event; enforced by the persistence layer, not by
subscriber control flow"):

    ALTER TABLE notifications ADD COLUMN source_event_id uuid NULL;

    CREATE UNIQUE INDEX uq_notifications_user_id_source_event_id
      ON notifications (user_id, source_event_id)
     WHERE source_event_id IS NOT NULL;

``source_event_id`` is the outbox ``event.id`` that produced the row — the relay's
own idempotency coordinate. It is **nullable** (non-event notifications — future
welcome/system messages — leave it NULL and must coexist freely) with **no FK** to
``event_outbox`` (the outbox is transient transport state that may be pruned/parked;
the id is a logical dedupe key, not a lifetime coupling). The unique index is
therefore **partial** (``WHERE source_event_id IS NOT NULL``) so multiple NULL rows
are permitted while ``(user_id, source_event_id)`` is unique for event-derived rows.

``(user_id, source_event_id)`` (not ``source_event_id`` alone) guarantees exactly-once
**per recipient per event** while keeping the door open for one event fanning out to
multiple recipients later — today every export event has exactly one recipient
(``requested_by_user_id``), so the semantics are unchanged.

Additive and safe: a new nullable column + a partial unique index on a currently
**empty** table (no rows to backfill or conflict). Plain (transactional) DDL — not
``CONCURRENTLY`` — because Alembic wraps each migration in a transaction and the
table is empty in every current environment. ``downgrade`` drops both, so each
ci_gate upgrade→downgrade→upgrade roundtrip (stages 5–7) runs on a clean slate.

Revision ID: 0009_notifications_source_event_id
Revises: 0008_projects_pagination_index
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "0009_notifications_source_event_id"
down_revision: str | None | Sequence[str] = "0008_projects_pagination_index"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


_TABLE = "notifications"
_COLUMN = "source_event_id"
_INDEX_NAME = "uq_notifications_user_id_source_event_id"


def upgrade() -> None:
    """Add the nullable ``source_event_id`` column + partial unique dedupe index."""
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, PgUUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        _INDEX_NAME,
        _TABLE,
        ["user_id", _COLUMN],
        unique=True,
        postgresql_where=sa.text("source_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the partial unique index + the ``source_event_id`` column."""
    op.drop_index(_INDEX_NAME, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
