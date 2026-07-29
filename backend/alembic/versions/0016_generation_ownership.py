"""α9.7 — Generation Ingress (owner-scoped generations + durable request payload).

See: docs/decisions/ADR-0052-generation-ingress-ownership-execution-and-read-contract.md
     docs/engineering/PHASE3_ALPHA9_7_PREFLIGHT.md §3
     docs/engineering/EXECUTION_RUNTIME_CONTRACT.md §3

Resolves the ownership deferral recorded by ADR-0046 Q1 ("generation has no tenant/owner/
project/publishing context **yet**") and α8.8 AP9 ("project-asserted, generation-unowned").
A creator-triggered generation must be attributable to the creator who asked for it, so:

    ALTER TABLE generations
      ADD COLUMN tenant_id       uuid REFERENCES tenants(id) ON DELETE RESTRICT,
      ADD COLUMN owner_user_id   uuid REFERENCES users(id)   ON DELETE RESTRICT,
      ADD COLUMN idempotency_key text,
      ADD COLUMN request         jsonb NOT NULL DEFAULT '{}'::jsonb;

    CREATE INDEX ix_generations_owner_created
      ON generations (owner_user_id, created_at DESC, id DESC)
     WHERE owner_user_id IS NOT NULL;

    CREATE UNIQUE INDEX uq_generations_owner_idempotency_key
      ON generations (owner_user_id, idempotency_key)
     WHERE owner_user_id IS NOT NULL AND idempotency_key IS NOT NULL;

**Why nullable** (ADR-0052 D1, "Migration philosophy — legacy ownerless generations"):
historical rows are legacy *implementation artefacts*, not user-visible domain objects — they
were produced by ``scripts/generate_demo.py`` and the test suite while the runtime had no
identity context at all, so they never represented a creator's request. Their owner is
genuinely unknown and **must never be inferred, attributed heuristically, or backfilled**.
Nullable columns + partial indexes + owner-predicated reads make them simply *invisible* to
the production API, which preserves ownership correctness rather than losing data (the rows
remain intact for administrative inspection outside ``/api/v1``). Tightening to NOT NULL is a
deliberate later migration once no legacy rows remain.

**ON DELETE RESTRICT** mirrors ``publish_jobs`` (0014): a generation is a spend record, so
deleting a tenant/user must not silently erase it.

**The owner-read index** is ``(owner_user_id, created_at DESC, id DESC)`` so it matches the
keyset order of ``GET /api/v1/generations`` exactly (newest first over the ``(created_at, id)``
total order — ``app.application.pagination``), making the list a pure index scan.

**The idempotency index is partial** on both columns being non-NULL: legacy (owner-less) rows
and callers who supplied no key coexist freely. This gives render-job semantics
(``uq_render_jobs_project_id_idempotency_key``, 0001) scoped to the *owner* rather than a
project, and — per ADR-0048's posture — lets the constraint own the concurrent-create race
instead of application-level dedup.

**``request``** is the creator's asserted intent, persisted verbatim so a worker that claims a
queued row can reconstruct the ``GenerateVideoRequest`` exactly (several request fields —
``target_duration_seconds``, ``per_shot_seconds``, ``min_similarity``, ``max_attempts`` — have
no column). This is the ``publish_jobs.content_package`` pattern: the durable, immutable job
payload lives with the job. It is ingress-owned and the execution runtime never writes it
(pre-flight GEN-1).

Additive and safe: four nullable/defaulted columns + two partial indexes. ``downgrade`` drops
both indexes then all four columns, so each ci_gate upgrade→downgrade→upgrade roundtrip
(stages 5-7) runs on a clean slate.

Revision ID: 0016_generation_ownership
Revises: 0015_analytics_events_source_event_id
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = "0016_generation_ownership"
down_revision: str | None | Sequence[str] = "0015_analytics_events_source_event_id"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


_TABLE = "generations"
_OWNER_READ_INDEX = "ix_generations_owner_created"
_IDEMPOTENCY_INDEX = "uq_generations_owner_idempotency_key"


def upgrade() -> None:
    """Add the ingress-owned ownership/idempotency/request columns + their indexes."""
    op.add_column(
        _TABLE,
        sa.Column(
            "tenant_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "owner_user_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(_TABLE, sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("request", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    # Owner-scoped keyset list: column order + DESC direction mirror the query's ORDER BY
    # (created_at DESC, id DESC) so paging is a pure index scan. Partial, because legacy
    # rows carry no owner and must never appear in an owner-scoped read.
    op.create_index(
        _OWNER_READ_INDEX,
        _TABLE,
        [sa.text("owner_user_id"), sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )
    # DB-enforced create idempotency (ADR-0052 D4): the constraint decides the concurrent
    # -create race, never application dedup.
    op.create_index(
        _IDEMPOTENCY_INDEX,
        _TABLE,
        ["owner_user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL AND idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop both indexes + the four ingress-owned columns."""
    op.drop_index(_IDEMPOTENCY_INDEX, table_name=_TABLE)
    op.drop_index(_OWNER_READ_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "request")
    op.drop_column(_TABLE, "idempotency_key")
    op.drop_column(_TABLE, "owner_user_id")
    op.drop_column(_TABLE, "tenant_id")
