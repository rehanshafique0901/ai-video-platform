"""Frozen component version identifiers for generation provenance.

Every generation records the exact revision of each pipeline component that
produced it (EXECUTION_RUNTIME_CONTRACT.md §5), so a defect found later is
attributable to a specific component version. Bump the relevant constant
whenever a component's *observable behaviour* changes.

The resolver + score-schema versions live with the resolver domain
(``app.domain.resolver``) and are re-exported here for a single provenance
import surface.
"""

from __future__ import annotations

from app.domain.resolver import RESOLVER_VERSION, SCORE_SCHEMA_VERSION

PLANNER_VERSION = "planner/1.0"
STORYBOARD_VERSION = "storyboard/1.0"
PROMPT_BUILDER_VERSION = "prompt_builder/1.0"
VERIFIER_VERSION = "verifier/1.0"
REPAIR_VERSION = "repair/1.0"
RENDERER_VERSION = "slideshow/1.0"

__all__ = [
    "PLANNER_VERSION",
    "STORYBOARD_VERSION",
    "PROMPT_BUILDER_VERSION",
    "VERIFIER_VERSION",
    "REPAIR_VERSION",
    "RENDERER_VERSION",
    "RESOLVER_VERSION",
    "SCORE_SCHEMA_VERSION",
]
