"""Unit tests — α9.7 generation request codec (pre-flight PF3).

A misread request would silently generate something the creator never asked for, and bill them
as if they had. So the codec round-trips exactly, and fails loudly on anything it does not
recognise rather than quietly falling back to a default.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.request_codec import (
    SPEC_VERSION,
    GenerationRequestSpec,
    decode_spec,
    encode_spec,
    to_runtime_request,
)
from app.core.errors import ValidationFailedError
from app.domain.generation.execution import ExecutionMode
from app.domain.generation.identity import GlobalStyle, IdentityProfile

pytestmark = pytest.mark.unit


def test_round_trips_every_field() -> None:
    spec = GenerationRequestSpec(
        prompt="a fox in the snow",
        seed=99,
        title="Fox",
        execution_mode=ExecutionMode.LOCAL_ONLY.value,
        global_style=GlobalStyle.ANIME.value,
        aspect_ratio="16:9",
        target_platform="youtube",
        target_duration_seconds=24.0,
        per_shot_seconds=4.0,
        width=1920,
        height=1080,
        fps=24,
    )

    assert decode_spec(encode_spec(spec)) == spec


def test_defaults_match_the_runtime_request_defaults() -> None:
    """An otherwise-empty spec must reconstruct what a direct caller builds by hand."""
    identity = IdentityProfile(seed=1)
    default = GenerateVideoRequest(prompt="p", identity=identity)
    built = to_runtime_request(GenerationRequestSpec(prompt="p", seed=1), generation_id=None)

    assert built.aspect_ratio == default.aspect_ratio
    assert built.target_platform == default.target_platform
    assert built.target_duration_seconds == default.target_duration_seconds
    assert built.per_shot_seconds == default.per_shot_seconds
    assert (built.width, built.height, built.fps) == (default.width, default.height, default.fps)
    assert built.execution_mode is default.execution_mode


def test_builds_the_v1_identity_from_seed_and_style() -> None:
    spec = GenerationRequestSpec(prompt="p", seed=7, global_style=GlobalStyle.WATERCOLOR.value)
    gen_id = uuid4()

    request = to_runtime_request(spec, generation_id=gen_id)

    assert request.generation_id == gen_id
    assert request.identity.seed == 7
    assert request.identity.global_style is GlobalStyle.WATERCOLOR
    # v1 scope: no characters, locations, props or references are authored yet.
    assert request.identity.characters == ()
    assert request.identity.locations == ()
    assert request.identity.props == ()
    assert request.identity.references == ()


def test_rejects_unknown_keys() -> None:
    """A later identity slice must extend the payload, never reinterpret an old one."""
    payload = encode_spec(GenerationRequestSpec(prompt="p", seed=1))
    payload["characters"] = [{"id": "c1"}]

    with pytest.raises(ValidationFailedError):
        decode_spec(payload)


def test_rejects_an_unsupported_version() -> None:
    payload = encode_spec(GenerationRequestSpec(prompt="p", seed=1))
    payload["v"] = SPEC_VERSION + 1

    with pytest.raises(ValidationFailedError):
        decode_spec(payload)


def test_rejects_a_payload_missing_a_required_field() -> None:
    with pytest.raises(ValidationFailedError):
        decode_spec({"v": SPEC_VERSION, "prompt": "p"})
