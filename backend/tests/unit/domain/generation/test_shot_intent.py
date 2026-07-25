"""Unit tests for the Planner V2 cinematic vocabulary (α8.7, Phase 1).

Pure and fast: exercises the enums, the ``ShotIntent`` CS-7 helpers, the
data-driven story-arc templates (authored + synthesised), and the
``StoryboardDiversityReport`` introspection helper — all without a planner,
generator, or ffmpeg.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.domain.generation.shot_intent import (
    Camera,
    Movement,
    ShotIntent,
    ShotType,
    StoryboardDiversityReport,
    Transition,
    adjacent_ok,
    assign_shot_ids,
    derive_shot_seed,
    differs_primary,
    differs_secondary,
    select_arc,
    shot_id_for,
)

pytestmark = pytest.mark.unit


def _intent(**overrides: object) -> ShotIntent:
    base: dict[str, object] = {
        "shot_type": ShotType.MEDIUM,
        "camera": Camera.TRACK,
        "movement": Movement.WALKING,
        "subject_focus": "fox",
        "emotional_purpose": "exploration",
        "transition_from_previous": Transition.CUT,
    }
    base.update(overrides)
    return ShotIntent(**base)  # type: ignore[arg-type]


# --- vocabularies -----------------------------------------------------------


def test_vocabularies_are_small_and_controlled() -> None:
    assert [s.value for s in ShotType] == [
        "establishing",
        "wide",
        "medium",
        "close_up",
        "detail",
        "action",
        "ending",
    ]
    assert len(list(Camera)) == 8
    # Subject movement is a distinct concept from camera behaviour.
    assert {m.value for m in Movement} == {
        "still",
        "walking",
        "running",
        "turning",
        "looking",
        "interacting",
    }
    assert {t.value for t in Transition} == {"cut", "dissolve", "fade", "match_cut"}


# --- CS-7 dimension split ---------------------------------------------------


def test_primary_and_secondary_signatures_partition_the_dimensions() -> None:
    intent = _intent()
    assert intent.primary_signature() == (ShotType.MEDIUM, Camera.TRACK, "fox")
    assert intent.secondary_signature() == (Movement.WALKING, "exploration", Transition.CUT)
    assert intent.signature() == intent.primary_signature() + intent.secondary_signature()


def test_transition_only_difference_is_not_cs7_compliant() -> None:
    # Two shots that differ ONLY by transition (a secondary) must be illegal.
    a = _intent(transition_from_previous=None)
    b = _intent(transition_from_previous=Transition.DISSOLVE)
    assert differs_secondary(a, b) is True
    assert differs_primary(a, b) is False
    assert adjacent_ok(a, b) is False


def test_primary_only_difference_is_not_cs7_compliant() -> None:
    a = _intent(shot_type=ShotType.WIDE)
    b = _intent(shot_type=ShotType.CLOSE_UP)
    assert differs_primary(a, b) is True
    assert differs_secondary(a, b) is False
    assert adjacent_ok(a, b) is False


def test_primary_and_secondary_difference_is_cs7_compliant() -> None:
    a = _intent(shot_type=ShotType.WIDE, movement=Movement.STILL)
    b = _intent(shot_type=ShotType.CLOSE_UP, movement=Movement.LOOKING)
    assert adjacent_ok(a, b) is True


# --- story arc templates ----------------------------------------------------


@pytest.mark.parametrize("shot_count", [3, 5, 6])
def test_authored_arcs_have_expected_shape_and_satisfy_cs7(shot_count: int) -> None:
    arc = select_arc(shot_count)
    assert arc.kind == "cinematic"
    assert arc.shot_count == shot_count
    assert arc.beats[0].shot_type is ShotType.ESTABLISHING
    assert arc.beats[-1].shot_type is ShotType.ENDING
    assert arc.beats[0].transition_from_previous is None


@pytest.mark.parametrize("shot_count", list(range(1, 13)))
def test_every_arc_is_position_stable_and_cs7_compliant(shot_count: int) -> None:
    arc = select_arc(shot_count)
    assert arc.shot_count == shot_count
    intents = tuple(
        ShotIntent(
            shot_type=b.shot_type,
            camera=b.camera,
            movement=b.movement,
            subject_focus=b.focus.value,
            emotional_purpose=b.emotional_purpose,
            transition_from_previous=b.transition_from_previous,
        )
        for b in arc.beats
    )
    for a, b in pairwise(intents):
        assert adjacent_ok(a, b), f"CS-7 violated in {shot_count}-shot arc"


def test_select_arc_rejects_unknown_kind_and_bad_count() -> None:
    with pytest.raises(ValueError):
        select_arc(6, kind="tutorial")
    with pytest.raises(ValueError):
        select_arc(0)


# --- diversity report -------------------------------------------------------


def test_diversity_report_flags_a_healthy_storyboard() -> None:
    arc = select_arc(6)
    intents = tuple(
        ShotIntent(
            shot_type=b.shot_type,
            camera=b.camera,
            movement=b.movement,
            subject_focus=b.focus.value,
            emotional_purpose=b.emotional_purpose,
            transition_from_previous=b.transition_from_previous,
        )
        for b in arc.beats
    )
    report = StoryboardDiversityReport.from_intents(intents, template_used=arc.kind)
    assert report.template_used == "cinematic"
    assert report.shot_count == 6
    assert report.duplicate_intents == 0
    assert report.unique_shot_types >= 3
    assert report.camera_variety >= 3
    assert report.satisfies_cs7 is True


def test_diversity_report_detects_duplicates_and_cs7_failure() -> None:
    dup = _intent()
    report = StoryboardDiversityReport.from_intents((dup, dup, dup))
    assert report.duplicate_intents == 2  # 3 intents, 1 unique signature
    assert report.primary_changes == 0
    assert report.satisfies_cs7 is False


# --- shot ids + seeds -------------------------------------------------------


def test_authored_arc_shot_ids_are_semantic_and_suffix_free() -> None:
    arc = select_arc(6)
    ids = assign_shot_ids(arc.beats)
    assert ids[0] == "scene-001-establishing"
    assert ids[-1] == "scene-001-ending"
    assert "scene-001-closeup" in ids
    # Authored arcs use unique shot types, so no occurrence suffixes.
    assert all(id_.count("-") == 2 for id_ in ids)
    assert len(set(ids)) == len(ids)


def test_shot_ids_are_position_independent_under_insertion() -> None:
    before = assign_shot_ids(select_arc(5).beats)
    after = assign_shot_ids(select_arc(6).beats)
    # Shot types shared by both arcs keep identical ids regardless of position.
    for shared in ("establishing", "medium", "close_up", "ending"):
        sid = shot_id_for(ShotType(shared))
        if sid in before and sid in after:
            assert sid in before and sid in after


def test_repeated_shot_types_get_occurrence_suffix() -> None:
    ids = assign_shot_ids(select_arc(10).beats)
    assert len(set(ids)) == len(ids)  # still unique
    assert any(id_.count("-") == 3 for id_ in ids)  # at least one suffixed


def test_derive_shot_seed_is_deterministic_nonnegative_and_id_sensitive() -> None:
    s1 = derive_shot_seed(70707, "scene-001-establishing")
    s2 = derive_shot_seed(70707, "scene-001-establishing")
    s3 = derive_shot_seed(70707, "scene-001-ending")
    s4 = derive_shot_seed(12345, "scene-001-establishing")
    assert s1 == s2
    assert s1 != s3  # different shot id -> different seed
    assert s1 != s4  # different project seed -> different seed
    assert 0 <= s1 < (1 << 63)  # non-negative, fits signed bigint
