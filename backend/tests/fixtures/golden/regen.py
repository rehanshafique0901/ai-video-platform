"""Regenerate the ACTIVE golden (``v2/fox_snowy_forest.json``) from the planner.

Run deliberately (``python -m tests.fixtures.golden.regen``) only when the
planner/storyboard behaviour changes *on purpose* — the golden file is a
regression guard, so an accidental diff here should fail CI, not be blessed
silently. The ``resolver`` section is authored by hand (it describes what the
e2e test seeds), so it is preserved across regenerations. The V1 golden under
``v1/`` is a frozen historical artifact and is never regenerated.
"""

from __future__ import annotations

import json

from app.domain.generation.identity import GlobalStyle
from app.domain.generation.planner import PlanRequest, plan_from_prompt
from app.domain.generation.storyboard import build_storyboard
from tests.fixtures.golden.scenario import (
    GOLDEN_JSON,
    GOLDEN_PROMPT,
    GOLDEN_SEED,
    fox_request,
)

_V2_COMMENT = (
    "ACTIVE golden — Golden V2 (Cinematic Planner, α8.7). The 'storyboard' section "
    "is the deterministic Planner V2 + Storyboard output and is asserted byte-for-byte "
    "by tests/unit/domain/generation/test_golden_scenario.py (which also asserts the "
    "cinematic-diversity invariants: distinct shots, CS-7 adjacency). The 'resolver' "
    "section is validated by the live end-to-end test and is authored by hand. "
    "Regenerate with: python -m tests.fixtures.golden.regen. The frozen minimal-planner "
    "snapshot lives in ../v1/fox_snowy_forest.json."
)


def build_document() -> dict[str, object]:
    existing = json.loads(GOLDEN_JSON.read_text()) if GOLDEN_JSON.exists() else {}
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
        "_comment": _V2_COMMENT,
        "request": {
            "prompt": GOLDEN_PROMPT,
            "seed": GOLDEN_SEED,
            "global_style": GlobalStyle.PIXAR.value,
            "execution_mode": req.execution_mode.value,
            "aspect_ratio": req.aspect_ratio,
            "target_platform": req.target_platform,
            "target_duration_seconds": req.target_duration_seconds,
            "per_shot_seconds": req.per_shot_seconds,
        },
        "storyboard": {
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
        },
        "resolver": existing.get("resolver", {}),
    }


def main() -> None:
    GOLDEN_JSON.write_text(json.dumps(build_document(), indent=2) + "\n")
    print(f"wrote {GOLDEN_JSON}")


if __name__ == "__main__":
    main()
