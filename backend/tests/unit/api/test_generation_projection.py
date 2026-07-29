"""Unit tests — the α9.7 public generation projection stays curated (ADR-0052 D3).

The direct descendant of α9.5's `_email` sanitisation test. There, internal bookkeeping leaked
into a public payload because nothing asserted it could not. Here the execution runtime keeps a
great deal a creator has no business seeing — resolver internals, adapter and provider
identities, component versions, the full provenance blob, and the internal
`final_video_asset_id` — and this test is what stops any of it arriving on the wire because
somebody widened a DTO.
"""

from __future__ import annotations

import pytest

from app.api.v1.schemas.generations import GenerationPublic

pytestmark = pytest.mark.unit

# Everything the execution plane records that must never appear in the public contract.
_FORBIDDEN_FIELDS = {
    "provenance",
    "chosen_provider",
    "chosen_adapter",
    "execution_tier",
    "execution_mode",
    "manifest_digest",
    "catalogue_version",
    "planner_version",
    "storyboard_version",
    "prompt_builder_version",
    "resolver_version",
    "verifier_version",
    "repair_version",
    "renderer_version",
    "score_schema_version",
    "final_video_asset_id",
    "video_backend",
    "video_bucket",
    "video_key",
    "identity_id",
    "seed",
    "request",
    "tenant_id",
    "owner_user_id",
    "idempotency_key",
}


def test_public_projection_exposes_no_runtime_internals() -> None:
    leaked = _FORBIDDEN_FIELDS & set(GenerationPublic.model_fields)

    assert leaked == set(), f"execution internals leaked into the public contract: {leaked}"


def test_public_projection_exposes_exactly_the_contracted_fields() -> None:
    """Pinned explicitly so widening the contract is a deliberate, reviewed edit."""
    assert set(GenerationPublic.model_fields) == {
        "id",
        "status",
        "prompt",
        "title",
        "aspect_ratio",
        "target_platform",
        "width",
        "height",
        "fps",
        "shot_count",
        "shots_accepted",
        "duration_seconds",
        "failure_reason",
        "promotable",
        "created_at",
        "started_at",
        "finished_at",
    }
