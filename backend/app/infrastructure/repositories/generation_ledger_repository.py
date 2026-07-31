"""α8.6 Increment 4 — generation ledger repository (raw SQL, ORM-less).

Writes the Execution Runtime aggregate (``generations``) and per-shot records
(``generation_shots``) created by migration ``0012``. Same pattern as the α8.5d/e
catalogue repositories: an :class:`AsyncSession`, module-level ``text()`` SQL, and
named params. Transaction scoping is the caller's (the Execution Runtime store
opens a short session + commits per step — generation is long-running).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.execution_runtime_store import ShotRecord
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import GenerationProvenance
from app.domain.generation.execution_state import ExecutionStatus

# GEN-1 (α9.7) — **state initialisation, not a generic upsert.**
#
# Since α9.7 a generation may already exist when the runtime starts: ingress inserts an owned
# `queued` row and a worker claims it before running the pipeline, so `begin()` must adopt that
# row rather than collide with it. It must equally still *create* the row for the direct-
# invocation path (`scripts/generate_demo.py`, integration tests), which is why this is an
# upsert at all.
#
# The DO UPDATE clause below enumerates **only runtime-owned execution columns**. `tenant_id`,
# `owner_user_id`, `idempotency_key`, `request`, `prompt`, `identity_id` and `created_at` are
# ingress-owned and are absent by construction — so a `begin()` call can never rebind a queued
# generation to a different owner, a different request or a different world, no matter how often
# it is replayed. `prompt` is supplied on INSERT (it is NOT NULL) but never on UPDATE: the
# creator's prompt is theirs.
#
# α10.0 (PF3) removed `identity_id` from this statement entirely — column list, values and
# DO UPDATE alike. It was written as `NULL` on every call, so an ingress-written world was
# erased by the first status write; and the execution runtime has no business knowing what a
# profile is. The world a run executes reaches it through the decoded request payload and
# nowhere else (ADR-0055 D2/D4, GEN-1).
#
# See docs/engineering/EXECUTION_RUNTIME_CONTRACT.md §4 and PHASE3_ALPHA9_7_PREFLIGHT.md §2.1.
_INSERT_GENERATION_SQL = text(
    """
    INSERT INTO generations (
        id, status, prompt, title, execution_mode, execution_tier,
        chosen_provider, chosen_adapter, seed, aspect_ratio, target_platform,
        width, height, fps, shot_count,
        planner_version, storyboard_version, prompt_builder_version, resolver_version,
        verifier_version, repair_version, renderer_version, score_schema_version,
        catalogue_version, manifest_digest, provenance, started_at
    ) VALUES (
        CAST(:id AS uuid), CAST(:status AS generation_status), :prompt, :title,
        :execution_mode, CAST(:execution_tier AS execution_tier),
        :chosen_provider, :chosen_adapter, :seed, :aspect_ratio, :target_platform,
        :width, :height, :fps, :shot_count,
        :planner_version, :storyboard_version, :prompt_builder_version, :resolver_version,
        :verifier_version, :repair_version, :renderer_version, :score_schema_version,
        :catalogue_version, :manifest_digest, CAST(:provenance AS jsonb), :started_at
    )
    ON CONFLICT (id) DO UPDATE SET
        status                 = EXCLUDED.status,
        title                  = EXCLUDED.title,
        execution_mode         = EXCLUDED.execution_mode,
        execution_tier         = EXCLUDED.execution_tier,
        chosen_provider        = EXCLUDED.chosen_provider,
        chosen_adapter         = EXCLUDED.chosen_adapter,
        seed                   = EXCLUDED.seed,
        aspect_ratio           = EXCLUDED.aspect_ratio,
        target_platform        = EXCLUDED.target_platform,
        width                  = EXCLUDED.width,
        height                 = EXCLUDED.height,
        fps                    = EXCLUDED.fps,
        shot_count             = EXCLUDED.shot_count,
        planner_version        = EXCLUDED.planner_version,
        storyboard_version     = EXCLUDED.storyboard_version,
        prompt_builder_version = EXCLUDED.prompt_builder_version,
        resolver_version       = EXCLUDED.resolver_version,
        verifier_version       = EXCLUDED.verifier_version,
        repair_version         = EXCLUDED.repair_version,
        renderer_version       = EXCLUDED.renderer_version,
        score_schema_version   = EXCLUDED.score_schema_version,
        catalogue_version      = EXCLUDED.catalogue_version,
        manifest_digest        = EXCLUDED.manifest_digest,
        provenance             = EXCLUDED.provenance,
        started_at             = EXCLUDED.started_at,
        updated_at             = now()
    """
)

_UPDATE_STATUS_SQL = text(
    """
    UPDATE generations
       SET status = CAST(:status AS generation_status), updated_at = now()
     WHERE id = CAST(:id AS uuid)
    """
)

_MARK_COMPLETED_SQL = text(
    """
    UPDATE generations
       SET status = 'completed',
           final_video_asset_id = CAST(:final_video_asset_id AS uuid),
           video_backend = CAST(:video_backend AS storage_backend),
           video_bucket = :video_bucket,
           video_key = :video_key,
           duration_seconds = :duration_seconds,
           width = COALESCE(:width, width),
           height = COALESCE(:height, height),
           finished_at = :finished_at,
           updated_at = now()
     WHERE id = CAST(:id AS uuid)
    """
)

_MARK_FAILED_SQL = text(
    """
    UPDATE generations
       SET status = 'failed', failure_reason = :reason,
           finished_at = :finished_at, updated_at = now()
     WHERE id = CAST(:id AS uuid)
    """
)

_INSERT_SHOT_SQL = text(
    """
    INSERT INTO generation_shots (
        generation_id, shot_number, prompt, negative_prompt, reference_images,
        adapter_used, seed, accepted, verification, attempts, repair_count,
        asset_id, reason
    ) VALUES (
        CAST(:generation_id AS uuid), :shot_number, :prompt, :negative_prompt,
        CAST(:reference_images AS jsonb), :adapter_used, :seed, :accepted,
        CAST(:verification AS jsonb), CAST(:attempts AS jsonb), :repair_count,
        CAST(:asset_id AS uuid), :reason
    )
    ON CONFLICT (generation_id, shot_number) DO UPDATE SET
        prompt = EXCLUDED.prompt,
        negative_prompt = EXCLUDED.negative_prompt,
        reference_images = EXCLUDED.reference_images,
        adapter_used = EXCLUDED.adapter_used,
        seed = EXCLUDED.seed,
        accepted = EXCLUDED.accepted,
        verification = EXCLUDED.verification,
        attempts = EXCLUDED.attempts,
        repair_count = EXCLUDED.repair_count,
        asset_id = EXCLUDED.asset_id,
        reason = EXCLUDED.reason
    """
)


class GenerationLedgerRepository:
    """Raw-SQL writer for ``generations`` + ``generation_shots``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_generation(
        self,
        *,
        generation_id: UUID,
        request: GenerateVideoRequest,
        provenance: GenerationProvenance,
        title: str,
        shot_count: int,
        status: ExecutionStatus = ExecutionStatus.PLANNING,
    ) -> None:
        """Initialise runtime state for a generation, creating the row only if absent (GEN-1).

        Idempotent: repeated calls for the same ``generation_id`` converge on the same runtime
        state and never touch ingress-owned columns.
        """
        await self._session.execute(
            _INSERT_GENERATION_SQL,
            {
                "id": str(generation_id),
                "status": status.value,
                "prompt": request.prompt,
                "title": title,
                "execution_mode": request.execution_mode.value,
                "execution_tier": provenance.execution_tier,
                "chosen_provider": provenance.chosen_provider,
                "chosen_adapter": provenance.chosen_adapter,
                "seed": request.identity.seed,
                "aspect_ratio": request.aspect_ratio,
                "target_platform": request.target_platform,
                "width": request.width,
                "height": request.height,
                "fps": request.fps,
                "shot_count": shot_count,
                "planner_version": provenance.planner_version,
                "storyboard_version": provenance.storyboard_version,
                "prompt_builder_version": provenance.prompt_builder_version,
                "resolver_version": provenance.resolver_version,
                "verifier_version": provenance.verifier_version,
                "repair_version": provenance.repair_version,
                "renderer_version": provenance.renderer_version,
                "score_schema_version": provenance.score_schema_version,
                "catalogue_version": provenance.catalogue_version,
                "manifest_digest": provenance.manifest_digest,
                "provenance": json.dumps(_provenance_payload(provenance)),
                "started_at": datetime.now(UTC),
            },
        )

    async def update_status(self, *, generation_id: UUID, status: ExecutionStatus) -> None:
        await self._session.execute(
            _UPDATE_STATUS_SQL, {"id": str(generation_id), "status": status.value}
        )

    async def insert_shot(self, shot: ShotRecord) -> None:
        await self._session.execute(
            _INSERT_SHOT_SQL,
            {
                "generation_id": str(shot.generation_id),
                "shot_number": shot.shot_number,
                "prompt": shot.prompt,
                "negative_prompt": shot.negative_prompt,
                "reference_images": json.dumps(list(shot.reference_images)),
                "adapter_used": shot.adapter_used,
                "seed": shot.seed,
                "accepted": shot.accepted,
                "verification": json.dumps(shot.verification),
                "attempts": json.dumps(list(shot.attempts)),
                "repair_count": shot.repair_count,
                "asset_id": str(shot.asset_id) if shot.asset_id else None,
                "reason": shot.reason,
            },
        )

    async def mark_completed(
        self,
        *,
        generation_id: UUID,
        final_video_asset_id: UUID,
        video_backend: str,
        video_bucket: str,
        video_key: str,
        duration_seconds: float | None,
        width: int | None,
        height: int | None,
    ) -> None:
        await self._session.execute(
            _MARK_COMPLETED_SQL,
            {
                "id": str(generation_id),
                "final_video_asset_id": str(final_video_asset_id),
                "video_backend": video_backend,
                "video_bucket": video_bucket,
                "video_key": video_key,
                "duration_seconds": duration_seconds,
                "width": width,
                "height": height,
                "finished_at": datetime.now(UTC),
            },
        )

    async def mark_failed(self, *, generation_id: UUID, reason: str) -> None:
        await self._session.execute(
            _MARK_FAILED_SQL,
            {"id": str(generation_id), "reason": reason, "finished_at": datetime.now(UTC)},
        )


def _provenance_payload(provenance: GenerationProvenance) -> dict[str, Any]:
    """Flatten provenance into the ``generations.provenance`` JSONB blob."""
    return {
        "capability": provenance.capability,
        "execution_mode": provenance.execution_mode,
        "execution_tier": provenance.execution_tier,
        "chosen_adapter": provenance.chosen_adapter,
        "chosen_provider": provenance.chosen_provider,
        "catalogue_version": provenance.catalogue_version,
        "manifest_digest": provenance.manifest_digest,
        "candidate_adapters": list(provenance.candidate_adapters),
        "versions": {
            "planner": provenance.planner_version,
            "storyboard": provenance.storyboard_version,
            "prompt_builder": provenance.prompt_builder_version,
            "resolver": provenance.resolver_version,
            "verifier": provenance.verifier_version,
            "repair": provenance.repair_version,
            "renderer": provenance.renderer_version,
            "score_schema": provenance.score_schema_version,
        },
    }
