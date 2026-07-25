"""Unit tests for the pure row/payload mappers of the Execution Runtime repos."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.generation.results import GenerationProvenance
from app.infrastructure.repositories.generation_ledger_repository import _provenance_payload
from app.infrastructure.repositories.model_cache_repository import cached_model_from_row

pytestmark = pytest.mark.unit


def test_provenance_payload_flattens_component_versions() -> None:
    prov = GenerationProvenance(
        generation_id=uuid4(),
        capability="image_generation",
        execution_mode="auto",
        resolver_version="resolver/1.0",
        chosen_adapter="pollinations.image",
        chosen_provider="pollinations",
        execution_tier="free_remote",
        catalogue_version="2026.07",
        manifest_digest="abc123",
        candidate_adapters=("pollinations.image", "fal.image"),
        planner_version="planner/1.0",
        storyboard_version="storyboard/1.0",
        prompt_builder_version="prompt_builder/1.0",
        verifier_version="verifier/1.0",
        repair_version="repair/1.0",
        renderer_version="slideshow/1.0",
        score_schema_version=1,
    )

    payload = _provenance_payload(prov)

    assert payload["capability"] == "image_generation"
    assert payload["execution_tier"] == "free_remote"
    assert payload["candidate_adapters"] == ["pollinations.image", "fal.image"]
    assert payload["versions"] == {
        "planner": "planner/1.0",
        "storyboard": "storyboard/1.0",
        "prompt_builder": "prompt_builder/1.0",
        "resolver": "resolver/1.0",
        "verifier": "verifier/1.0",
        "repair": "repair/1.0",
        "renderer": "slideshow/1.0",
        "score_schema": 1,
    }


def test_cached_model_from_row_maps_all_fields() -> None:
    model = cached_model_from_row(
        {
            "model_ref": "comfyui/flux-schnell",
            "version": "1.0",
            "backend": "metal",
            "execution_tier": "local",
            "local_path": "/cache/flux",
            "status": "ready",
            "supported_capabilities": ["image_generation"],
        }
    )
    assert model.model_ref == "comfyui/flux-schnell"
    assert model.execution_tier == "local"
    assert model.local_path == "/cache/flux"
    assert model.supported_capabilities == ("image_generation",)


def test_cached_model_from_row_tolerates_missing_optionals() -> None:
    model = cached_model_from_row({"model_ref": "x", "status": "registered"})
    assert model.version is None
    assert model.local_path is None
    assert model.supported_capabilities == ()
