"""Generation use cases — the Execution plane of the AI generation slice.

``GenerateVideo`` composes the pure Decision-plane policies (planner, prompt
builder, verification, repair) with side-effecting ports (capability resolver,
image generator, feature extractor, slideshow renderer, video probe, object
storage, model cache) to turn a prompt into a stored short video with full
provenance. It is capability-first and provider-agnostic (ADR-0045): no adapter
is named in code and no scoring happens here.
"""

from __future__ import annotations

from app.application.use_cases.generation.generate_video import GenerateVideo
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import (
    AttemptRecord,
    GenerateVideoResult,
    GenerationProvenance,
    GenerationStatus,
    ShotResult,
)

__all__ = [
    "GenerateVideo",
    "GenerateVideoRequest",
    "GenerateVideoResult",
    "GenerationProvenance",
    "GenerationStatus",
    "ShotResult",
    "AttemptRecord",
]
