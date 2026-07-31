"""α10.0 — Identity Runtime (creator-authored world state).

See: docs/decisions/ADR-0055-identity-runtime-authoring.md
     docs/engineering/PHASE3_ALPHA10_0_PREFLIGHT.md §3

Four new tables give a creator somewhere durable to keep a *world* — named
characters, the place they are in, recurring props, and the project look — so
that the shots of a video are about the same people in the same place. The
runtime already consumes all of it (``domain/generation/identity.py`` →
``prompt_builder``); until now there was no way to author any of it.

**Relational, not a JSONB body** (PF1). This is *mutable* creator state: one
character gets edited without rewriting the world, a child's stable key must be
unique inside its profile, and the database owns that uniqueness. JSONB in this
repository is reserved for frozen snapshots — ``generations.request`` and
``publish_jobs.content_package`` — which is exactly what the *binding* of a
world to a generation becomes (ADR-0055 D2), not the source.

**Owner-scoped, not project-scoped** (PF9): ``generations`` itself carries only
``tenant_id`` + ``owner_user_id`` (0016), so coupling a world to a project would
invent a relationship the consuming side cannot use. ``ON DELETE RESTRICT``
mirrors ``media_assets`` / ``library_folders``.

**The root owns the version** (PF8). Children are written through the profile
and every child mutation bumps the root's ``version``, because a snapshot must
never straddle two states. Hence the OCC trigger pair on the parent only —
``touch_updated_at`` + the guarded ``bump_version`` (both from 0001) — and no
per-child version.

**Children cascade** from the profile: a hard delete of a world removes its
characters, locations and props (PF10). Generations that already bound that
world are unaffected — they hold a snapshot, not a reference, and their
``identity_id`` stays behind as a provenance value that no longer resolves
(IDENT-1).

**``global_style`` is text, not a Postgres ENUM.** A new enum type is a
governance-visible addition (``tests/test_enums.py`` requires an ADR, a registry
entry and a count bump), and ADR-0055 authorises none. The value is validated by
``GlobalStyle`` in the domain and at the authoring surface, and it already
travels as a string in ``generations.request``.

**Deliberately absent columns** (PF5, and ADR-0055 frozen decision 19): no
reference images, no voice, personality, expressions, poses, music or subtitle
style — no v1 path consumes them. And never, in any later migration: execution
history, planner decisions, adapter or provider preference, adapter health,
success statistics, or verification outcomes. A profile records what the creator
declares exists, never what happened.

``downgrade`` drops the children, then the parent, so each ci_gate
upgrade→downgrade→upgrade roundtrip (stages 5-7) starts from a clean slate.

Revision ID: 0017_identity_runtime
Revises: 0016_generation_ownership
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_identity_runtime"
down_revision: str | None | Sequence[str] = "0016_generation_ownership"
branch_labels: str | None | Sequence[str] = None
depends_on: str | None | Sequence[str] = None


def upgrade() -> None:
    """Create the profile root, its three child tables, and the root's OCC triggers."""
    op.execute(
        """
        CREATE TABLE identity_profiles (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            owner_user_id    uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            name             text NOT NULL,
            seed             bigint NOT NULL,
            global_style     text NOT NULL DEFAULT 'pixar',
            camera_style     text,
            lighting         text,
            color_palette    text,
            negative_prompt  text,
            version          integer NOT NULL DEFAULT 1,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One world per name per creator: the creator names a world when they request a
    # generation, so the name has to identify it unambiguously.
    op.execute(
        "CREATE UNIQUE INDEX uq_identity_profiles_owner_name "
        "ON identity_profiles (owner_user_id, name)"
    )
    # Owner-scoped keyset list: column order + DESC direction mirror the query's
    # ORDER BY (created_at DESC, id DESC), as ix_generations_owner_created does.
    op.execute(
        "CREATE INDEX ix_identity_profiles_owner_created "
        "ON identity_profiles (owner_user_id, created_at DESC, id DESC)"
    )

    op.execute(
        """
        CREATE TABLE identity_characters (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id     uuid NOT NULL REFERENCES identity_profiles(id) ON DELETE CASCADE,
            character_key  text NOT NULL,
            name           text NOT NULL,
            age            text,
            appearance     text[] NOT NULL DEFAULT '{}',
            clothing       text,
            accessories    text[] NOT NULL DEFAULT '{}',
            "position"     integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity_locations (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id    uuid NOT NULL REFERENCES identity_profiles(id) ON DELETE CASCADE,
            location_key  text NOT NULL,
            name          text NOT NULL,
            descriptors   text[] NOT NULL DEFAULT '{}',
            "position"    integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity_props (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id   uuid NOT NULL REFERENCES identity_profiles(id) ON DELETE CASCADE,
            prop_key     text NOT NULL,
            name         text NOT NULL,
            descriptors  text[] NOT NULL DEFAULT '{}',
            "position"   integer NOT NULL DEFAULT 0
        )
        """
    )
    # The stable key the planner and shot records carry, unique inside its profile —
    # so a rename of `name` never breaks a reference. Each index also serves the
    # profile_id lookup, so the children need no separate FK index.
    op.execute(
        "CREATE UNIQUE INDEX uq_identity_characters_profile_key "
        "ON identity_characters (profile_id, character_key)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_identity_locations_profile_key "
        "ON identity_locations (profile_id, location_key)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_identity_props_profile_key "
        "ON identity_props (profile_id, prop_key)"
    )

    # OCC + audit triggers on the root only (PF8) — mirrors publish_jobs (0014).
    # The guarded bump no-ops when a CAS update already hand-sets version = version + 1,
    # so the net increment stays +1 whether the edit came through the root or a child.
    op.execute(
        "CREATE TRIGGER tg_identity_profiles_biu_touch_updated_at "
        "BEFORE UPDATE ON identity_profiles FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER tg_identity_profiles_biu_version_bump "
        "BEFORE UPDATE ON identity_profiles FOR EACH ROW EXECUTE FUNCTION bump_version()"
    )


def downgrade() -> None:
    """Drop the children, then the parent."""
    op.execute("DROP TABLE IF EXISTS identity_characters CASCADE")
    op.execute("DROP TABLE IF EXISTS identity_locations CASCADE")
    op.execute("DROP TABLE IF EXISTS identity_props CASCADE")
    op.execute("DROP TABLE IF EXISTS identity_profiles CASCADE")
