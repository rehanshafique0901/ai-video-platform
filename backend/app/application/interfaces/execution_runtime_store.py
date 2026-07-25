"""Port: the persistent Execution Runtime store (α8.6 Increment 4).

The Execution plane (``GenerateVideo``) persists its lifecycle through this port:
the generation aggregate + state machine (``generations``), per-shot records
(``generation_shots``), the artefact registry (``generation_assets``), and the
resolution ledger (``generation_resolution_ledger``, reused). Concrete
implementations write via short raw-SQL transactions (generation is long-running,
so no single transaction spans the whole run) and emit lifecycle events through
the transactional outbox co-transactionally with each state change.

Tests use a fake that records calls in memory; the use case never sees a session.
See ``docs/engineering/EXECUTION_RUNTIME_CONTRACT.md`` (invariants W8.6.1–8).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.application.interfaces.capability_resolver import CapabilityResolution
from app.application.interfaces.resolution_ledger import ExecutionOutcome
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import GenerationProvenance
from app.domain.generation.execution_state import ExecutionStatus, GenerationAssetKind


@dataclass(frozen=True, slots=True)
class NewGenerationAsset:
    """An execution artefact to register in ``generation_assets``.

    ``parent_asset_id`` turns repair/upscale/face-fix into a lineage graph rather
    than an overwrite (contract §3).
    """

    generation_id: UUID
    asset_kind: GenerationAssetKind
    storage_backend: str
    storage_bucket: str
    storage_key: str
    mime_type: str
    shot_number: int | None = None
    size_bytes: int | None = None
    checksum_sha256: bytes | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    parent_asset_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShotRecord:
    """A completed per-shot execution record for ``generation_shots``."""

    generation_id: UUID
    shot_number: int
    prompt: str
    accepted: bool
    negative_prompt: str | None = None
    reference_images: tuple[str, ...] = ()
    adapter_used: str | None = None
    seed: int | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    attempts: tuple[dict[str, Any], ...] = ()
    repair_count: int = 0
    asset_id: UUID | None = None
    reason: str | None = None


class IExecutionRuntimeStore(ABC):
    """Persist the generation lifecycle + artefacts + provenance."""

    @abstractmethod
    async def begin(
        self,
        *,
        generation_id: UUID,
        request: GenerateVideoRequest,
        provenance: GenerationProvenance,
        title: str,
        shot_count: int,
    ) -> None:
        """Insert the ``generations`` aggregate (status ``PLANNING``) + emit start."""
        ...

    @abstractmethod
    async def set_status(self, *, generation_id: UUID, status: ExecutionStatus) -> None:
        """Advance the persisted state machine (contract §4)."""
        ...

    @abstractmethod
    async def record_resolution(
        self,
        *,
        generation_id: UUID,
        resolution: CapabilityResolution,
        outcome: ExecutionOutcome,
    ) -> None:
        """Append the full ranked resolution to the resolution ledger."""
        ...

    @abstractmethod
    async def register_asset(self, asset: NewGenerationAsset) -> UUID:
        """Register one artefact in ``generation_assets``; return its id."""
        ...

    @abstractmethod
    async def record_shot(self, shot: ShotRecord) -> None:
        """Persist a per-shot record + emit the matching lifecycle event(s)."""
        ...

    @abstractmethod
    async def complete(
        self,
        *,
        generation_id: UUID,
        final_video_asset_id: UUID,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
        duration_seconds: float | None,
        width: int | None,
        height: int | None,
    ) -> None:
        """Mark the generation ``COMPLETED`` + emit render/export events."""
        ...

    @abstractmethod
    async def fail(self, *, generation_id: UUID, reason: str) -> None:
        """Mark the generation ``FAILED`` (terminal) with a reason."""
        ...


class NullExecutionRuntimeStore(IExecutionRuntimeStore):
    """No-op store: the use case runs without persistence (default / simple tests)."""

    async def begin(
        self,
        *,
        generation_id: UUID,
        request: GenerateVideoRequest,
        provenance: GenerationProvenance,
        title: str,
        shot_count: int,
    ) -> None:
        return None

    async def set_status(self, *, generation_id: UUID, status: ExecutionStatus) -> None:
        return None

    async def record_resolution(
        self,
        *,
        generation_id: UUID,
        resolution: CapabilityResolution,
        outcome: ExecutionOutcome,
    ) -> None:
        return None

    async def register_asset(self, asset: NewGenerationAsset) -> UUID:
        from uuid import uuid4

        return uuid4()

    async def record_shot(self, shot: ShotRecord) -> None:
        return None

    async def complete(
        self,
        *,
        generation_id: UUID,
        final_video_asset_id: UUID,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
        duration_seconds: float | None,
        width: int | None,
        height: int | None,
    ) -> None:
        return None

    async def fail(self, *, generation_id: UUID, reason: str) -> None:
        return None
