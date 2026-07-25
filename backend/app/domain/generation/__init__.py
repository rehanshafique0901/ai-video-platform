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
immutable ``IdentityProfile`` — the project *world state* of characters,
locations, props, and the project-wide look (camera, lighting, colour palette,
global style) plus a stable seed. Every shot's prompt is composed from it via the
**Prompt Builder** (`prompt_builder`), so generation extends a fixed identity
instead of re-inventing one per frame. Execution modes (`execution`) express the
capability-first selection policy (local / free-remote / commercial) without
naming any machine or vendor.
"""

from __future__ import annotations

from app.domain.generation.execution import (
    ExecutionConstraints,
    ExecutionMode,
    ExecutionTier,
    constraints_for,
)
from app.domain.generation.identity import (
    Character,
    GlobalStyle,
    IdentityProfile,
    Location,
    Prop,
    ReferenceImage,
    ReferenceKind,
    join_fragments,
)
from app.domain.generation.plan import GenerationPlan, Shot
from app.domain.generation.planner import PlanRequest, plan_from_prompt
from app.domain.generation.prompt_builder import build_prompt
from app.domain.generation.repair import RepairAction, RepairDecision, decide_repair
from app.domain.generation.shot_intent import (
    Camera,
    FocusHint,
    Movement,
    ShotIntent,
    ShotType,
    StoryArcTemplate,
    StoryboardDiversityReport,
    TemplateBeat,
    Transition,
    adjacent_ok,
    assign_shot_ids,
    derive_shot_seed,
    differs_primary,
    differs_secondary,
    select_arc,
    shot_id_for,
)
from app.domain.generation.storyboard import ShotPrompt, build_storyboard
from app.domain.generation.timeline_verification import (
    TimelineFrame,
    TimelineReport,
    verify_timeline,
)
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
    "Location",
    "Prop",
    "ReferenceImage",
    "ReferenceKind",
    "join_fragments",
    # prompt builder
    "build_prompt",
    # execution modes
    "ExecutionConstraints",
    "ExecutionMode",
    "ExecutionTier",
    "constraints_for",
    # plan
    "GenerationPlan",
    "Shot",
    # planner
    "PlanRequest",
    "plan_from_prompt",
    # shot intent (Planner V2 / α8.7)
    "Camera",
    "FocusHint",
    "Movement",
    "ShotIntent",
    "ShotType",
    "StoryArcTemplate",
    "StoryboardDiversityReport",
    "TemplateBeat",
    "Transition",
    "adjacent_ok",
    "assign_shot_ids",
    "derive_shot_seed",
    "differs_primary",
    "differs_secondary",
    "select_arc",
    "shot_id_for",
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
    # timeline verification
    "TimelineFrame",
    "TimelineReport",
    "verify_timeline",
    # repair
    "RepairAction",
    "RepairDecision",
    "decide_repair",
]
