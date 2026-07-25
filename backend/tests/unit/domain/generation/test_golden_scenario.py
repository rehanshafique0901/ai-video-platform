"""Golden regression test — the cinematic planner/storyboard must stay deterministic.

Fast (no DB, no ffmpeg): replays the checked-in golden scenario through the pure
Decision-plane policies and asserts the storyboard matches the ACTIVE golden
(``v2/fox_snowy_forest.json``) byte-for-byte. This is the compiler-style golden
test — any accidental change to the planner or storyboard shows up here as a diff.
When the change is intentional, run ``python -m tests.fixtures.golden.regen`` and
commit the new golden.

It also asserts the α8.7 cinematic-diversity invariants (distinct shots, CS-7
adjacency) on the active golden, and keeps the frozen V1 snapshot honest as history.
"""

from __future__ import annotations

import json

import pytest

from app.domain.generation.planner import PlanRequest, plan_from_prompt
from app.domain.generation.shot_intent import StoryboardDiversityReport, validate_adjacency
from app.domain.generation.storyboard import build_storyboard
from tests.fixtures.golden.scenario import GOLDEN_JSON, GOLDEN_V1_JSON, fox_request

pytestmark = pytest.mark.unit


def _plan():
    req = fox_request()
    return plan_from_prompt(
        PlanRequest(
            prompt=req.prompt,
            identity=req.identity,
            aspect_ratio=req.aspect_ratio,
            target_platform=req.target_platform,
            target_duration_seconds=req.target_duration_seconds,
            per_shot_seconds=req.per_shot_seconds,
            title=req.title,
        )
    )


def _storyboard_doc() -> dict[str, object]:
    plan = _plan()
    storyboard = build_storyboard(plan)
    return {
        "title": plan.title,
        "shot_count": len(storyboard),
        "shots": [
            {
                "index": s.index,
                "seed": s.seed,
                "duration_seconds": s.duration_seconds,
                "prompt_text": s.prompt_text,
                "negative_prompt": s.negative_prompt,
                "reference_image_refs": list(s.reference_image_refs),
            }
            for s in storyboard
        ],
    }


def test_golden_storyboard_matches_active_v2_fixture() -> None:
    golden = json.loads(GOLDEN_JSON.read_text())
    assert _storyboard_doc() == golden["storyboard"]


def test_golden_storyboard_is_deterministic() -> None:
    # Replaying the same request must produce identical orchestration decisions.
    assert _storyboard_doc() == _storyboard_doc()


def test_active_golden_is_cinematically_diverse() -> None:
    plan = _plan()
    intents = tuple(s.intent for s in plan.shots if s.intent is not None)
    validate_adjacency(intents)  # CS-7 holds for the whole storyboard
    report = StoryboardDiversityReport.from_intents(intents)
    assert report.duplicate_intents == 0
    assert report.unique_shot_types >= 3
    assert report.camera_variety >= 3
    assert report.satisfies_cs7 is True

    # The concrete storyboard must yield distinct prompts and distinct seeds — the
    # very thing the α8.6 duplicate-frame failure lacked.
    doc = _storyboard_doc()
    prompts = [s["prompt_text"] for s in doc["shots"]]  # type: ignore[index]
    seeds = [s["seed"] for s in doc["shots"]]  # type: ignore[index]
    assert len(set(prompts)) == len(prompts)
    assert len(set(seeds)) == len(seeds)


def test_v1_golden_is_frozen_duplicate_scene_history() -> None:
    """V1 is kept only as history: the duplicate-scene 'before' state.

    This documents *why* α8.7 exists and guards against anyone accidentally editing
    the frozen artifact — it must remain the single-prompt, single-seed snapshot and
    must no longer match what the live cinematic planner produces.
    """
    v1 = json.loads(GOLDEN_V1_JSON.read_text())["storyboard"]
    v1_prompts = {s["prompt_text"] for s in v1["shots"]}
    v1_seeds = {s["seed"] for s in v1["shots"]}
    assert len(v1_prompts) == 1  # every V1 shot shared one prompt
    assert len(v1_seeds) == 1  # ...and one seed (the duplicate-frame cause)

    # The live planner has moved on — it no longer reproduces V1.
    assert _storyboard_doc() != v1
