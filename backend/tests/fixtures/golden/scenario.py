"""The golden scenario — one canonical generation request.

A single source of truth for the "little red fox" scenario so the fast unit
golden test (planner + storyboard determinism), the live end-to-end integration
test (full pipeline + persistence + reproducibility), and the demo CLI all drive
*identical* inputs.

Golden versions (see ``CINEMATIC_STORYBOARD_CONTRACT.md`` §15):
  * ``v1/`` — FROZEN history: the α8.6 minimal planner (one prompt/seed per shot).
    Never replayed by the live planner; kept only as an architectural snapshot.
  * ``v2/`` — the ACTIVE regression suite: the α8.7 cinematic planner. Every future
    architecture change replays this scenario, the way a compiler keeps a golden.

The scenario pins ``FREE_REMOTE_ONLY`` so selection is deterministic regardless
of what local/commercial adapters exist in the seeded catalogue at the time — the
resolver can only pick a free-remote adapter, and the e2e test seeds one that
outscores the catalogue so the chosen adapter is stable and inspectable.
"""

from __future__ import annotations

from pathlib import Path

from app.application.use_cases.generation.request import GenerateVideoRequest
from app.domain.generation.execution import ExecutionMode
from app.domain.generation.identity import Character, GlobalStyle, IdentityProfile, Location

_HERE = Path(__file__).parent
# The active golden is V2 (cinematic planner); V1 is a frozen historical artifact.
GOLDEN_JSON = _HERE / "v2" / "fox_snowy_forest.json"
GOLDEN_V1_JSON = _HERE / "v1" / "fox_snowy_forest.json"

GOLDEN_PROMPT = "A little red fox walking through a snowy forest at sunrise."
GOLDEN_SEED = 70707


def fox_identity() -> IdentityProfile:
    """The frozen identity/world-state for the golden scenario.

    Planner V2 (α8.7) governs framing per shot via ``ShotIntent``, so the project
    look no longer hardcodes a framing-specific ``camera_style`` (which would fight
    a per-shot close-up); it contributes only non-framing atmosphere.
    """
    return IdentityProfile(
        seed=GOLDEN_SEED,
        global_style=GlobalStyle.PIXAR,
        characters=(Character(id="fox", name="a little red fox", clothing=""),),
        locations=(Location(id="forest", name="a snowy forest at sunrise"),),
        lighting="soft sunrise light",
    )


def fox_request(**overrides: object) -> GenerateVideoRequest:
    """Build the golden request. ``overrides`` allow a test to vary one field."""
    params: dict[str, object] = {
        "prompt": GOLDEN_PROMPT,
        "identity": fox_identity(),
        "execution_mode": ExecutionMode.FREE_REMOTE_ONLY,
        "aspect_ratio": "9:16",
        "target_platform": "reel",
        "target_duration_seconds": 18.0,
        "per_shot_seconds": 3.0,
    }
    params.update(overrides)
    return GenerateVideoRequest(**params)  # type: ignore[arg-type]
