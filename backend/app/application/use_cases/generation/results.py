"""Result + provenance objects for the ``GenerateVideo`` use case."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class GenerationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One generate+verify attempt for a shot (verifier-first provenance)."""

    attempt: int
    seed: int
    verification_passed: bool
    action: str  # RepairAction value
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ShotResult:
    index: int
    accepted: bool
    frame_key: str | None
    seed: int | None
    attempts: tuple[AttemptRecord, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class GenerationProvenance:
    """Everything needed to explain/replay why this render was produced.

    Mirrors the resolution ledger fields (ADR-0044) plus the full component
    version set (EXECUTION_RUNTIME_CONTRACT.md §5) so the Execution plane can
    persist a complete, attributable provenance record.
    """

    generation_id: UUID
    capability: str
    execution_mode: str
    resolver_version: str
    chosen_adapter: str | None = None
    chosen_provider: str | None = None
    execution_tier: str | None = None
    catalogue_version: str | None = None
    manifest_digest: str | None = None
    candidate_adapters: tuple[str, ...] = ()
    # Component versions (frozen in app.domain.generation.versions).
    planner_version: str | None = None
    storyboard_version: str | None = None
    prompt_builder_version: str | None = None
    verifier_version: str | None = None
    repair_version: str | None = None
    renderer_version: str | None = None
    score_schema_version: int | None = None


@dataclass(frozen=True, slots=True)
class GenerateVideoResult:
    status: GenerationStatus
    generation_id: UUID
    title: str
    provenance: GenerationProvenance
    shots: tuple[ShotResult, ...] = ()
    video_key: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    reason: str = ""
    checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        return self.status is GenerationStatus.SUCCEEDED
