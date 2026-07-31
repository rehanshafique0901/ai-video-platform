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
    SUPPORTED_SPEC_VERSIONS,
    CharacterSnapshot,
    EntitySnapshot,
    GenerationRequestSpec,
    IdentitySnapshot,
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


# --- v2: the authored world, captured at acceptance (α10.0, PF4) ------------


def _snapshot(**overrides: object) -> IdentitySnapshot:
    base: dict[str, object] = {
        "identity_id": str(uuid4()),
        "version": 3,
        "name": "Bedtime world",
        "seed": 4242,
        "global_style": GlobalStyle.ANIME.value,
        "camera_style": "handheld",
        "lighting": "golden hour",
        "color_palette": "warm pastels",
        "negative_prompt": "no text",
        "characters": (
            CharacterSnapshot(
                key="zoe",
                name="Zoe",
                age="7 years old",
                appearance=("curly red hair",),
                clothing="yellow raincoat",
                accessories=("green boots",),
            ),
        ),
        "locations": (EntitySnapshot(key="home", name="Home", descriptors=("cosy",)),),
        "props": (EntitySnapshot(key="kite", name="Kite", descriptors=("red",)),),
    }
    base.update(overrides)
    return IdentitySnapshot(**base)  # type: ignore[arg-type]


def test_the_current_version_is_two() -> None:
    assert SPEC_VERSION == 2
    assert SUPPORTED_SPEC_VERSIONS == (1, 2)


def test_a_v1_row_still_decodes_and_carries_no_world() -> None:
    """The rows α9.7 queued must replay exactly as written — never rewritten, never guessed."""
    v1_payload = {
        "v": 1,
        "prompt": "a paper boat",
        "seed": 5,
        "title": "Boat",
        "execution_mode": ExecutionMode.AUTO.value,
        "global_style": GlobalStyle.PIXAR.value,
        "aspect_ratio": "9:16",
        "target_platform": "reel",
        "target_duration_seconds": 18.0,
        "per_shot_seconds": 3.0,
        "width": 720,
        "height": 1280,
        "fps": 30,
    }

    spec = decode_spec(v1_payload)

    assert spec.identity is None
    assert spec == GenerationRequestSpec(prompt="a paper boat", seed=5, title="Boat")


def test_a_v1_row_decodes_to_exactly_the_identity_it_always_did() -> None:
    v1_payload = {"v": 1, "prompt": "p", "seed": 7, "global_style": GlobalStyle.DISNEY.value}

    request = to_runtime_request(decode_spec(v1_payload), generation_id=None)

    assert request.identity == IdentityProfile(seed=7, global_style=GlobalStyle.DISNEY)


def test_a_v1_row_may_not_carry_a_world() -> None:
    """A payload that claims version 1 and an identity is malformed, not "nearly v2"."""
    with pytest.raises(ValidationFailedError):
        decode_spec({"v": 1, "prompt": "p", "seed": 1, "identity": {}})


def test_a_v2_spec_without_a_world_omits_the_key_entirely() -> None:
    payload = encode_spec(GenerationRequestSpec(prompt="p", seed=1))

    assert payload["v"] == 2
    assert "identity" not in payload
    assert decode_spec(payload).identity is None


def test_a_v2_spec_without_a_world_behaves_exactly_as_v1() -> None:
    spec = GenerationRequestSpec(prompt="p", seed=7, global_style=GlobalStyle.WATERCOLOR.value)

    v2 = to_runtime_request(spec, generation_id=None)
    v1 = to_runtime_request(
        decode_spec({"v": 1, **{k: v for k, v in encode_spec(spec).items() if k != "v"}}),
        generation_id=None,
    )

    assert v2.identity == v1.identity


def test_a_world_round_trips_whole() -> None:
    spec = GenerationRequestSpec(prompt="p", seed=1, identity=_snapshot())

    assert decode_spec(encode_spec(spec)) == spec


def test_the_runtime_profile_is_rebuilt_from_the_snapshot_alone() -> None:
    snapshot = _snapshot()
    spec = GenerationRequestSpec(
        prompt="p", seed=1, global_style=GlobalStyle.PIXAR.value, identity=snapshot
    )

    identity = to_runtime_request(spec, generation_id=None).identity

    # The child's stable key becomes the id the planner and prompt builder address.
    assert [c.id for c in identity.characters] == ["zoe"]
    assert identity.characters[0].appearance == ("curly red hair",)
    assert identity.characters[0].accessories == ("green boots",)
    assert [loc.id for loc in identity.locations] == ["home"]
    assert [p.id for p in identity.props] == ["kite"]
    assert identity.camera_style == "handheld"
    assert identity.lighting == "golden hour"
    assert identity.color_palette == "warm pastels"
    assert identity.negative_prompt == "no text"


def test_the_run_uses_the_seed_ingress_resolved_not_the_world_s_own() -> None:
    """One authority per value: ``generations.seed`` and the runtime seed cannot disagree."""
    spec = GenerationRequestSpec(prompt="p", seed=11, identity=_snapshot(seed=4242))

    identity = to_runtime_request(spec, generation_id=None).identity

    assert identity.seed == 11
    assert spec.identity is not None and spec.identity.seed == 4242


def test_the_run_uses_the_style_ingress_resolved() -> None:
    spec = GenerationRequestSpec(
        prompt="p",
        seed=1,
        global_style=GlobalStyle.CLAYMATION.value,
        identity=_snapshot(global_style=GlobalStyle.ANIME.value),
    )

    assert to_runtime_request(spec, generation_id=None).identity.global_style is (
        GlobalStyle.CLAYMATION
    )


def test_an_empty_world_is_still_a_world() -> None:
    spec = GenerationRequestSpec(
        prompt="p",
        seed=1,
        identity=IdentitySnapshot(
            identity_id=str(uuid4()),
            version=1,
            name="Empty",
            seed=3,
            global_style=GlobalStyle.PIXAR.value,
        ),
    )

    decoded = decode_spec(encode_spec(spec))

    assert decoded == spec
    assert to_runtime_request(decoded, generation_id=None).identity.characters == ()


def test_rejects_an_unknown_key_inside_the_world() -> None:
    payload = encode_spec(GenerationRequestSpec(prompt="p", seed=1, identity=_snapshot()))
    payload["identity"]["music_style"] = "lofi"

    with pytest.raises(ValidationFailedError):
        decode_spec(payload)


def test_rejects_an_unknown_key_inside_a_character() -> None:
    """Reference images and voice are deferred (PF5) — a payload carrying one is malformed."""
    payload = encode_spec(GenerationRequestSpec(prompt="p", seed=1, identity=_snapshot()))
    payload["identity"]["characters"][0]["reference_image_refs"] = ["s3://x"]

    with pytest.raises(ValidationFailedError):
        decode_spec(payload)


def test_rejects_a_world_that_is_not_an_object() -> None:
    with pytest.raises(ValidationFailedError):
        decode_spec({"v": 2, "prompt": "p", "seed": 1, "identity": "bedtime"})


def test_rejects_a_world_missing_its_provenance() -> None:
    with pytest.raises(ValidationFailedError):
        decode_spec({"v": 2, "prompt": "p", "seed": 1, "identity": {"name": "Bedtime"}})


def test_rejects_a_cast_that_is_not_a_list() -> None:
    payload = encode_spec(GenerationRequestSpec(prompt="p", seed=1, identity=_snapshot()))
    payload["identity"]["characters"] = {"key": "zoe", "name": "Zoe"}

    with pytest.raises(ValidationFailedError):
        decode_spec(payload)


def test_rejects_an_appearance_that_is_not_a_list() -> None:
    payload = encode_spec(GenerationRequestSpec(prompt="p", seed=1, identity=_snapshot()))
    payload["identity"]["characters"][0]["appearance"] = "curly red hair"

    with pytest.raises(ValidationFailedError):
        decode_spec(payload)
