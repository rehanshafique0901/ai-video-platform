"""Port: generation ingress, owner-scoped reads, claiming and reaping (α9.7 / ADR-0052).

The counterpart to :class:`~app.application.interfaces.execution_runtime_store.IExecutionRuntimeStore`,
split along the ownership boundary the slice establishes:

* **This port is ingress-owned.** It creates the `queued` row with its owner and its immutable
  request, serves the owner-scoped read model, claims rows for execution, and terminalises rows
  abandoned by a crashed worker.
* **`IExecutionRuntimeStore` is runtime-owned.** It writes execution state only and never touches
  an ingress-owned column (pre-flight GEN-1).

Like the runtime store, implementations own their own short transactions rather than joining the
platform Unit of Work: `generations` is intentionally ORM-less (ADR-0046 Q2), and a generation run
is far too long for a single transaction to span.

**Owner-scoped reads are the sole supported read model** (ADR-0052 D1). Legacy rows written before
this slice carry no owner; they are invisible here by construction, and no method infers, guesses,
or backfills an owner for them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.application.pagination import Cursor
from app.application.use_cases.generation.request_codec import GenerationRequestSpec


@dataclass(frozen=True, slots=True)
class GenerationView:
    """The curated owner-facing projection of a generation (ADR-0052 D3).

    Deliberately **not** a row dump. `provenance`, the resolution ledger, the chosen
    adapter/provider, every component version, and `final_video_asset_id` are runtime internals
    and never cross the wire — the ADR-0051 read-model hygiene lesson applied to a second
    context. `promotable` is the derived signal a client actually needs: whether
    `POST /media/promotions` will succeed for this generation.
    """

    id: UUID
    status: str
    prompt: str
    title: str | None
    aspect_ratio: str | None
    target_platform: str | None
    width: int | None
    height: int | None
    fps: int | None
    shot_count: int | None
    shots_accepted: int
    duration_seconds: float | None
    failure_reason: str | None
    promotable: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class CreatedGeneration:
    """The outcome of an ingress create: the row, plus whether *this* call created it."""

    view: GenerationView
    created: bool


@dataclass(frozen=True, slots=True)
class ClaimedGeneration:
    """A row this worker has just won (`queued` → `planning`), with its stored request."""

    generation_id: UUID
    spec: GenerationRequestSpec


class CancelOutcome(StrEnum):
    """Result of an owner-scoped cancel (pre-flight PF4: queued-only in v1)."""

    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"
    NOT_CANCELLABLE = "not_cancellable"


class IGenerationJobStore(ABC):
    """Ingress-owned persistence for `generations`: create, read, claim, cancel, reap."""

    # ---- ingress ---------------------------------------------------------- #

    @abstractmethod
    async def create(
        self,
        *,
        generation_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        spec: GenerationRequestSpec,
        idempotency_key: str | None,
    ) -> CreatedGeneration:
        """Insert an owned `queued` generation, or return the idempotent replay.

        When `idempotency_key` is supplied and already used by this owner, the existing row is
        returned with `created=False`. The unique index — not application dedup — decides the
        concurrent-create race (ADR-0048 posture): a loser re-reads the winner.
        """
        ...

    # ---- owner-scoped reads ----------------------------------------------- #

    @abstractmethod
    async def get_owned(
        self, *, tenant_id: UUID, owner_user_id: UUID, generation_id: UUID
    ) -> GenerationView | None:
        """Read one of the caller's generations; `None` if missing **or** not owned."""
        ...

    @abstractmethod
    async def list_owned(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        cursor: Cursor | None = None,
        status: str | None = None,
    ) -> list[GenerationView]:
        """Keyset page of the caller's generations, newest first over `(created_at, id)`."""
        ...

    # ---- worker ----------------------------------------------------------- #

    @abstractmethod
    async def list_claimable(self, *, limit: int) -> list[UUID]:
        """FIFO scan of `queued` ids, oldest first — a server-side consumer, not owner-scoped."""
        ...

    @abstractmethod
    async def claim(self, *, generation_id: UUID) -> ClaimedGeneration | None:
        """CAS `queued` → `planning`, returning the stored request; `None` if already claimed.

        This CAS *is* the `max_attempts = 1` enforcement (ADR-0052 D2): nothing ever writes a
        row back to `queued`, so there is no requeue path to disable.
        """
        ...

    @abstractmethod
    async def list_reapable(self, *, stale_before: datetime, limit: int) -> list[UUID]:
        """Ids that were claimed, never reached a terminal state, and have gone quiet.

        A healthy worker refreshes `updated_at` at every phase transition, so the staleness
        cutoff distinguishes a long run from an abandoned one even if a lease renewal is missed.
        """
        ...

    @abstractmethod
    async def mark_lost(self, *, generation_id: UUID, reason: str) -> bool:
        """CAS non-terminal → `failed`. Terminalises an abandoned run; never re-runs it."""
        ...

    # ---- owner-scoped cancel ---------------------------------------------- #

    @abstractmethod
    async def cancel_queued(
        self, *, tenant_id: UUID, owner_user_id: UUID, generation_id: UUID
    ) -> CancelOutcome:
        """CAS `queued` → `cancelled` for the caller's row (PF4: only before it is claimed)."""
        ...
