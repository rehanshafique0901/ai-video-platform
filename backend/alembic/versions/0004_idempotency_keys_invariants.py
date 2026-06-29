"""Phase 3 Wave 1.2 — promote ``idempotency_keys`` mutability tracking and
status<->response invariant to the database.

See: docs/decisions/ADR-0031-idempotency-keys-invariants.md

Three coordinated changes, applied in a single migration so the table is
never observed in an intermediate partially-promoted state:

1. ``ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()`` — corrects
   the Phase 2 Step-A misclassification that placed ``idempotency_keys``
   under ``CreatedAtOnlyMixin`` even though the row IS mutated
   (``in_flight`` -> ``succeeded``/``failed``). ``DEFAULT now()`` backfills
   any existing rows atomically; production row count is 0 (verified by
   pre-upgrade SELECT — see ADR §Migration Plan / AC #3).

2. ``CREATE TRIGGER tg_idempotency_keys_biu_touch_updated_at`` — binds the
   already-existing shared ``touch_updated_at()`` PL/pgSQL function
   (defined in the baseline migration, lines 89-94, already wired to 30+
   tables via ``_UPDATED_AT_TABLES``) to this table. No new function is
   created; we are only adding a per-table BIU trigger that calls the
   existing function. The baseline migration's ``_UPDATED_AT_TABLES``
   tuple is intentionally NOT edited — baseline migrations are historical
   and never amended in place.

3. ``ADD CONSTRAINT chk_idempotency_keys_response_hash_matches_status`` —
   enforces the FSM invariant ``(status='in_flight') = (response_hash IS
   NULL)`` at the database level. PostgreSQL evaluates ``(bool) = (bool)``
   as biconditional (XNOR), so the CHECK passes iff exactly one of
   {``status='in_flight'`` and ``response_hash IS NULL``} or
   {``status IN ('succeeded','failed')`` and ``response_hash IS NOT
   NULL``} holds. The CHECK is scoped only to ``response_hash`` (the
   canonical witness of "response was computed"); see ADR §Decision and
   §Alternatives item 3 for why ``response_payload`` and ``http_status``
   are intentionally not part of the gate.

Hand-written rather than via ``alembic revision --autogenerate`` because
autogenerate does not emit ``CREATE TRIGGER`` statements at all, does not
preserve the exact text of CHECK expressions, and would lose the explicit
sequencing of the three ops (column first, then trigger, then CHECK).

Revision ID: 0004_idempotency_keys_invariants
Revises: 0003_export_jobs_partial_unique
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_idempotency_keys_invariants"
down_revision: str | None | Sequence[str] = "0003_export_jobs_partial_unique"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


_TABLE_NAME = "idempotency_keys"
_TRIGGER_NAME = "tg_idempotency_keys_biu_touch_updated_at"
_CHECK_NAME = "chk_idempotency_keys_response_hash_matches_status"
_CHECK_PREDICATE = "(status = 'in_flight') = (response_hash IS NULL)"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {_TABLE_NAME} "
        "ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute(
        f"CREATE TRIGGER {_TRIGGER_NAME} "
        f"BEFORE UPDATE ON {_TABLE_NAME} "
        "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )
    op.execute(
        f"ALTER TABLE {_TABLE_NAME} "
        f"ADD CONSTRAINT {_CHECK_NAME} "
        f"CHECK ({_CHECK_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {_TABLE_NAME} DROP CONSTRAINT {_CHECK_NAME}"
    )
    op.execute(
        f"DROP TRIGGER {_TRIGGER_NAME} ON {_TABLE_NAME}"
    )
    op.execute(
        f"ALTER TABLE {_TABLE_NAME} DROP COLUMN updated_at"
    )
