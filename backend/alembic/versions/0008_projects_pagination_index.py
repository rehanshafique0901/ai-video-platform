"""Phase 3 Slice α5b — composite partial index for the projects keyset scan (M3).

See: docs/engineering/PHASE3_ALPHA5B_PREFLIGHT.md §D10 / §D11
     docs/domain/PROJECT_AGGREGATE.md §3 (pagination)

Adds a single composite partial index

    CREATE INDEX ix_projects_owner_created_id
      ON projects (tenant_id, owner_user_id, created_at DESC, id DESC)
     WHERE deleted_at IS NULL;

that directly serves ``ProjectRepository.list_owned``'s query

    SELECT ... FROM projects
     WHERE tenant_id = :t AND owner_user_id = :o AND deleted_at IS NULL
     ORDER BY created_at DESC, id DESC
     LIMIT :n;

The leading equality columns (``tenant_id``, ``owner_user_id``) narrow the
scan to the caller's rows; the trailing ``created_at DESC, id DESC`` matches
the keyset ORDER BY exactly, so pagination becomes an index range scan with
no separate sort step. The index is *partial* (``WHERE deleted_at IS NULL``)
so soft-deleted rows — which ``list_owned`` never returns — do not bloat it.

Deferred from α5a §4.3 (no meaningful list volume yet); folded into α5b, the
slice that already touches the projects aggregate, per the reviewer's "don't
ship a standalone index-only migration" call. The existing non-partial
``ix_projects_tenant_id_owner_user_id`` is intentionally KEPT (α5b D10): it
may serve future include-deleted admin/restore scans, and dropping it is a
separate, riskier decision logged to backlog.

Plain (transactional) ``CREATE INDEX`` — not ``CONCURRENTLY`` — because
Alembic wraps each migration in a transaction and ``CONCURRENTLY`` cannot run
inside one; the ``projects`` table is tiny in every current environment so
the brief lock is a non-issue (α5b D11). ``CONCURRENTLY`` is a GA-scale
concern logged to backlog.

The CI gate exercises stages 5→6→7 (upgrade head → downgrade base → upgrade
head): ``downgrade`` drops the index so each ``upgrade`` runs on a clean
slate — a plain create/drop pair is idempotency-safe without ``IF [NOT]
EXISTS`` guards.

Revision ID: 0008_projects_pagination_index
Revises: 0007_usage_records_request_id_unique
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_projects_pagination_index"
down_revision: str | None | Sequence[str] = "0007_usage_records_request_id_unique"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


_INDEX_NAME = "ix_projects_owner_created_id"
_TABLE = "projects"


def upgrade() -> None:
    """Create the composite partial keyset index on ``projects``."""
    op.create_index(
        _INDEX_NAME,
        _TABLE,
        ["tenant_id", "owner_user_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Drop the composite partial keyset index."""
    op.drop_index(_INDEX_NAME, table_name=_TABLE)
