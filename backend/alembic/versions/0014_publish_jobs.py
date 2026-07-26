"""α8.6b — Publish Runtime (publish_jobs).

See: docs/engineering/PHASE3_ALPHA8_6b_PREFLIGHT.md (§2 data model, SIGNED OFF)
     docs/engineering/PUBLISHING_RUNTIME_CONTRACT.md (§6–§7, §12; PUB-1..PUB-10)

**Additive only.** Introduces the publish-execution table for the Publishing bounded
context — a faithful adaptation of ``export_jobs`` (DQ8: same OCC/versioning) plus a
second serialisation dimension (``project_id``), ``scheduled_at`` scheduling, and bounded
retries:

- ``publish_jobs`` — one user-initiated upload of a finished export-delivery ``MediaAsset``
                     (PUB-1) to one connected ``social_accounts`` destination (PUB-2).
                     Direct ownership (``tenant_id`` + ``requested_by_user_id``); an
                     explicit ``project_id`` powers the ``project_publish:<project_id>``
                     serialisation lock (DQ1). Idempotency is the partial-unique index on
                     ``(source_media_asset_id, social_account_id)`` over
                     ``status IN ('queued','running','succeeded')`` (DQ2). ``content_package``
                     is the deterministic metadata snapshot (PUB-9). Credentials are never
                     stored here — the worker fetches an ``AuthorizedContext`` from the α8.6a
                     credential service at run time (PUB-5).

ORM-backed (models in app/infrastructure/db/models/publishing.py), so NOT allowlisted as
ORM-less in validate_schema.py. ``downgrade`` drops the table (triggers cascade) + the enum
so the ci_gate upgrade→downgrade→upgrade roundtrip stays clean.

Revision ID: 0014_publish_jobs
Revises: 0013_social_accounts
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_publish_jobs"
down_revision: str | None | Sequence[str] = "0013_social_accounts"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE publish_status AS ENUM "
        "('queued', 'running', 'succeeded', 'failed', 'canceled')"
    )

    # ---- publish_jobs (user-initiated upload of a delivery MediaAsset) -------
    op.execute(
        """
        CREATE TABLE publish_jobs (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            requested_by_user_id   uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            project_id             uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_export_job_id   uuid NOT NULL REFERENCES export_jobs(id) ON DELETE CASCADE,
            source_media_asset_id  uuid NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,
            social_account_id      uuid NOT NULL REFERENCES social_accounts(id) ON DELETE CASCADE,
            platform               text NOT NULL,
            status                 publish_status NOT NULL,
            scheduled_at           timestamptz,
            attempt                integer NOT NULL DEFAULT 0,
            max_attempts           integer NOT NULL DEFAULT 5,
            content_package        jsonb NOT NULL,
            platform_post_id       text,
            platform_post_url      text,
            error                  jsonb,
            published_at           timestamptz,
            finished_at            timestamptz,
            version                integer NOT NULL DEFAULT 1,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # Idempotency backstop (DQ2, PUB-7): at most one active-or-fulfilled publish per
    # (source_media_asset_id, social_account_id). failed/canceled rows are excluded so a
    # retry after failure is permitted; succeeded is included so the same artifact is not
    # accidentally re-posted to the same account (an explicit re-publish is a future flow).
    op.execute(
        """
        CREATE UNIQUE INDEX uq_publish_jobs_source_media_asset_social_account
            ON publish_jobs (source_media_asset_id, social_account_id)
            WHERE status IN ('queued','running','succeeded')
        """
    )
    # Claim scan: queued + due, oldest first.
    op.execute(
        "CREATE INDEX ix_publish_jobs_status_scheduled_at ON publish_jobs(status, scheduled_at)"
    )
    op.execute(
        "CREATE INDEX ix_publish_jobs_requested_by_user_id_created_at "
        "ON publish_jobs(requested_by_user_id, created_at)"
    )
    op.execute("CREATE INDEX ix_publish_jobs_social_account_id ON publish_jobs(social_account_id)")

    # OCC + audit triggers — mirror export_jobs exactly (DQ8): touch_updated_at + the guarded
    # bump_version (both functions created in 0001_baseline). The guarded bump no-ops when a
    # CAS update already hand-sets version = version + 1, so the net increment stays +1.
    op.execute(
        "CREATE TRIGGER tg_publish_jobs_biu_touch_updated_at "
        "BEFORE UPDATE ON publish_jobs FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER tg_publish_jobs_biu_version_bump "
        "BEFORE UPDATE ON publish_jobs FOR EACH ROW EXECUTE FUNCTION bump_version()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS publish_jobs CASCADE")
    op.execute("DROP TYPE IF EXISTS publish_status")
