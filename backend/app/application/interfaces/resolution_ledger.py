"""α8.5e.5 — resolution ledger port (provenance).

Every generation resolution is recorded for complete replay (AR18 / W8.5e.5): the full
ranked candidate list — not just the winner — plus catalogue provenance
(`catalogue_version`, `manifest_digest`, `resolver_version`). This is an **Execution-plane**
collaborator (it captures `execution_result`, `end_time`, …); the resolver itself never
writes it (W8.5e.2/.3). Writes to `generation_resolution_ledger` (migration 0011).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.resolver import Resolution


class ExecutionOutcome(StrEnum):
    """Mirrors the `execution_result` Postgres enum (migration 0011)."""

    SUCCESS = "success"
    FAILURE = "failure"
    FALLBACK = "fallback"
    NONE = "none"


class IResolutionLedger(ABC):
    """Append-only writer for the generation resolution ledger."""

    @abstractmethod
    async def record(
        self,
        *,
        generation_id: UUID,
        resolution: Resolution,
        chosen_adapter: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        execution_result: ExecutionOutcome | None = None,
    ) -> UUID:
        """Persist one resolution and return the new ledger row id."""
        ...
