"""α8.6a — Publishing account connections (social_accounts + social_credentials).

See: docs/engineering/PHASE3_ALPHA8_6a_PREFLIGHT.md (§2 data model, SIGNED OFF)
     docs/decisions/ADR-0047-publishing-credential-ownership.md (C1–C8 / R1–R4)

**Additive only.** Introduces the publishing bounded context's account-connection tables:

- `social_accounts`    — non-secret connection profile + lifecycle status. `platform` is
                         free-text (OQ2); `status` is the new `social_account_status` enum.
                         Owner-scoped by (tenant_id, user_id); unique on
                         (user_id, platform, external_account_id) — multiple accounts per
                         (user, platform) (R4).
- `social_credentials` — envelope-encrypted OAuth tokens, 1:1 with an account. The database
                         holds only ciphertext + nonce + wrapped DEK + rotation metadata
                         (C1/C2) — never a plaintext/usable token. `access_token_expires_at`
                         is the only non-secret timing field (drives refresh).

These are **ORM-backed** (models in app/infrastructure/db/models/publishing.py), so they
are NOT allowlisted as ORM-less in validate_schema.py. `downgrade` drops both tables + the
enum so the ci_gate upgrade→downgrade→upgrade roundtrip stays clean.

Revision ID: 0013_social_accounts
Revises: 0012_execution_runtime
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_social_accounts"
down_revision: str | None | Sequence[str] = "0012_execution_runtime"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


def upgrade() -> None:
    op.execute("CREATE TYPE social_account_status AS ENUM ('connected', 'expired', 'revoked')")

    # ---- social_accounts (non-secret connection profile + status) -----------
    op.execute(
        """
        CREATE TABLE social_accounts (
            id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            user_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            platform             text NOT NULL,
            external_account_id  text NOT NULL,
            display_name         text,
            status               social_account_status NOT NULL DEFAULT 'connected',
            scopes               text[] NOT NULL DEFAULT '{}'::text[],
            connected_at         timestamptz,
            revoked_at           timestamptz,
            created_at           timestamptz NOT NULL DEFAULT now(),
            updated_at           timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_social_accounts_user_platform_external
                UNIQUE (user_id, platform, external_account_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_social_accounts_user_id ON social_accounts(user_id)")
    op.execute("CREATE INDEX ix_social_accounts_tenant_id ON social_accounts(tenant_id)")

    # ---- social_credentials (envelope-encrypted OAuth tokens; 1:1) ----------
    op.execute(
        """
        CREATE TABLE social_credentials (
            id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            social_account_id        uuid NOT NULL REFERENCES social_accounts(id) ON DELETE CASCADE,
            ciphertext               bytea NOT NULL,
            nonce                    bytea NOT NULL,
            wrapped_dek              bytea NOT NULL,
            key_version              text NOT NULL,
            algorithm                text NOT NULL DEFAULT 'AES-256-GCM',
            access_token_expires_at  timestamptz,
            rotated_at               timestamptz,
            created_at               timestamptz NOT NULL DEFAULT now(),
            updated_at               timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_social_credentials_social_account_id UNIQUE (social_account_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS social_credentials CASCADE")
    op.execute("DROP TABLE IF EXISTS social_accounts CASCADE")
    op.execute("DROP TYPE IF EXISTS social_account_status")
