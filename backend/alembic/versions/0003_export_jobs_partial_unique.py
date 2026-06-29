"""Phase 3 Wave 1.1 — promote export_jobs (render_job_id, format, quality,
orientation) uniqueness to a partial-unique DB constraint.

See: docs/decisions/ADR-0030-export-jobs-partial-unique.md

The partial predicate ``status IN ('queued','running','succeeded')`` scopes
uniqueness to the "active-or-fulfilled" set, leaving ``failed`` and
``canceled`` rows free so retries after a failed or cancelled export are
permitted. The ``export_jobs`` table intentionally has no ``deleted_at``
column (render/export jobs are operationally terminal, not soft-deletable
user objects — see schema.md §17); the partial scope is therefore on
``status``, not on a soft-delete flag.

Hand-written rather than via ``alembic revision --autogenerate`` because
autogenerate does not reliably emit partial-unique indexes via
``postgresql_where`` — it produces a vanilla unique constraint instead.

Revision ID: 0003_export_jobs_partial_unique
Revises: 0002_seed_system_data
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_export_jobs_partial_unique"
down_revision: str | None | Sequence[str] = "0002_seed_system_data"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


_INDEX_NAME = "uq_export_jobs_render_job_id_format_quality_orientation"
_TABLE_NAME = "export_jobs"
_COLUMNS = ("render_job_id", "format", "quality", "orientation")
_PARTIAL_WHERE = "status IN ('queued','running','succeeded')"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        _TABLE_NAME,
        list(_COLUMNS),
        unique=True,
        postgresql_where=sa.text(_PARTIAL_WHERE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
