"""Generation domain — the pure core of the AI generation vertical slice.

This package holds the **decision-plane** logic for turning a user prompt into a
verified, ordered set of shots ready for assembly into a short video. It is
pure (no I/O, no provider calls, no ffmpeg): every function is deterministic and
unit-testable. Side-effecting concerns — image generation, feature extraction,
ffmpeg assembly, storage, export — live behind ports in the application /
infrastructure layers and are orchestrated by a use case.

Per ADR-0045 (AI runtime core freeze):
  * the planner never chooses providers (it requests capabilities);
  * no provider-specific branching exists anywhere in this package;
  * verification and repair are pure policies over *observed features* that an
    infrastructure extractor produces — the policy decides pass/fail/retry, it
    never looks at raw bytes.

The **Identity Runtime** (`identity`) is the consistency backbone: a single
immutable ``IdentityProfile`` (characters, scene, objects, global style, stable
seed) that every shot's prompt references, so generation extends a fixed
identity instead of re-inventing one per frame.
"""

from __future__ import annotations

from app.domain.generation.identity import (
    Character,
    GlobalStyle,
    IdentityProfile,
    ObjectAsset,
    SceneStyle,
)
from app.domain.generation.plan import GenerationPlan, Shot
from app.domain.generation.planner import PlanRequest, plan_from_prompt
from app.domain.generation.repair import RepairAction, RepairDecision, decide_repair
from app.domain.generation.storyboard import ShotPrompt, build_storyboard
from app.domain.generation.verification import (
    CheckResult,
    CheckStatus,
    ObservedImage,
    VerificationExpectation,
    VerificationReport,
    verify_image,
)

__all__ = [
    # identity
    "Character",
    "GlobalStyle",
    "IdentityProfile",
    "ObjectAsset",
    "SceneStyle",
    # plan
    "GenerationPlan",
    "Shot",
    # planner
    "PlanRequest",
    "plan_from_prompt",
    # storyboard
    "ShotPrompt",
    "build_storyboard",
    # verification
    "CheckResult",
    "CheckStatus",
    "ObservedImage",
    "VerificationExpectation",
    "VerificationReport",
    "verify_image",
    # repair
    "RepairAction",
    "RepairDecision",
    "decide_repair",
]
