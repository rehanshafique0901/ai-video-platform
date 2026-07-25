"""Golden regression test — the planner/storyboard must stay deterministic.

Fast (no DB, no ffmpeg): replays the checked-in golden scenario through the pure
Decision-plane policies and asserts the storyboard matches ``fox_snowy_forest.json``
byte-for-byte. This is the compiler-style golden test — any accidental change to the
planner or storyboard shows up here as a diff. When the change is intentional, run
``python -m tests.fixtures.golden.regen`` and commit the new golden.
"""

from __future__ import annotations

import json

import pytest

from app.domain.generation.planner import PlanRequest, plan_from_prompt
from app.domain.generation.storyboard import build_storyboard
from tests.fixtures.golden.scenario import GOLDEN_JSON, fox_request

pytestmark = pytest.mark.unit


def _storyboard_doc() -> dict[str, object]:
    req = fox_request()
    plan = plan_from_prompt(
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


def test_golden_storyboard_matches_fixture() -> None:
    golden = json.loads(GOLDEN_JSON.read_text())
    assert _storyboard_doc() == golden["storyboard"]


def test_golden_storyboard_is_deterministic() -> None:
    # Replaying the same request must produce identical orchestration decisions.
    assert _storyboard_doc() == _storyboard_doc()
