"""In-memory fakes for the α9.7 generation-ingress unit tests.

Deliberately model the *concurrency* semantics rather than just the happy path: the store's
claim/cancel/reap operations are compare-and-swap in SQL, so the fakes enforce the same
preconditions. A test that would pass here only because the fake was permissive would be
worthless — the whole slice rests on those CASes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.application.interfaces.generation_job_store import (
    CancelOutcome,
    ClaimedGeneration,
    CreatedGeneration,
    GenerationView,
    IGenerationJobStore,
)
from app.application.interfaces.generation_runner import IGenerationRunner
from app.application.interfaces.locks import IDistributedLockManager, Lease
from app.application.pagination import Cursor
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.request_codec import GenerationRequestSpec
from app.application.use_cases.generation.results import (
    GenerateVideoResult,
    GenerationProvenance,
    GenerationStatus,
)
from app.domain.generation.execution_state import ExecutionStatus

TERMINAL = {
    ExecutionStatus.COMPLETED.value,
    ExecutionStatus.FAILED.value,
    ExecutionStatus.CANCELLED.value,
}


@dataclass
class _Row:
    """One stored generation: the ingress-owned half plus its mutable status."""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    spec: GenerationRequestSpec
    idempotency_key: str | None
    status: str = ExecutionStatus.QUEUED.value
    failure_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    shot_count: int | None = None
    shots_accepted: int = 0
    promotable: bool = False


class FakeGenerationJobStore(IGenerationJobStore):
    """In-memory :class:`IGenerationJobStore` with real CAS preconditions."""

    def __init__(self) -> None:
        self.rows: dict[UUID, _Row] = {}
        self.claim_calls: list[UUID] = []

    # ---- helpers for tests ------------------------------------------------ #

    def seed(
        self,
        *,
        tenant_id: UUID | None = None,
        owner_user_id: UUID | None = None,
        spec: GenerationRequestSpec | None = None,
        idempotency_key: str | None = None,
        status: str = ExecutionStatus.QUEUED.value,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> _Row:
        row = _Row(
            id=uuid4(),
            tenant_id=tenant_id or uuid4(),
            owner_user_id=owner_user_id or uuid4(),
            spec=spec or GenerationRequestSpec(prompt="a prompt", seed=1),
            idempotency_key=idempotency_key,
            status=status,
        )
        if created_at is not None:
            row.created_at = created_at
        if updated_at is not None:
            row.updated_at = updated_at
        self.rows[row.id] = row
        return row

    # ---- ingress ---------------------------------------------------------- #

    async def create(
        self,
        *,
        generation_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        spec: GenerationRequestSpec,
        idempotency_key: str | None,
    ) -> CreatedGeneration:
        if idempotency_key is not None:
            for row in self.rows.values():
                if row.owner_user_id == owner_user_id and row.idempotency_key == idempotency_key:
                    return CreatedGeneration(view=_view(row), created=False)
        row = _Row(
            id=generation_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            spec=spec,
            idempotency_key=idempotency_key,
        )
        self.rows[row.id] = row
        return CreatedGeneration(view=_view(row), created=True)

    # ---- reads ------------------------------------------------------------ #

    async def get_owned(
        self, *, tenant_id: UUID, owner_user_id: UUID, generation_id: UUID
    ) -> GenerationView | None:
        row = self.rows.get(generation_id)
        if row is None or row.tenant_id != tenant_id or row.owner_user_id != owner_user_id:
            return None
        return _view(row)

    async def list_owned(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        cursor: Cursor | None = None,
        status: str | None = None,
    ) -> list[GenerationView]:
        rows = [
            r
            for r in self.rows.values()
            if r.tenant_id == tenant_id and r.owner_user_id == owner_user_id
        ]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        if cursor is not None:
            rows = [r for r in rows if (r.created_at, r.id) < (cursor.created_at, cursor.id)]
        return [_view(r) for r in rows[:limit]]

    # ---- worker ----------------------------------------------------------- #

    async def list_claimable(self, *, limit: int) -> list[UUID]:
        rows = [r for r in self.rows.values() if r.status == ExecutionStatus.QUEUED.value]
        rows.sort(key=lambda r: (r.created_at, r.id))
        return [r.id for r in rows[:limit]]

    async def claim(self, *, generation_id: UUID) -> ClaimedGeneration | None:
        self.claim_calls.append(generation_id)
        row = self.rows.get(generation_id)
        if row is None or row.status != ExecutionStatus.QUEUED.value:
            return None
        row.status = ExecutionStatus.PLANNING.value
        row.updated_at = datetime.now(UTC)
        return ClaimedGeneration(generation_id=row.id, spec=row.spec)

    async def list_reapable(self, *, stale_before: datetime, limit: int) -> list[UUID]:
        rows = [
            r
            for r in self.rows.values()
            if r.status not in TERMINAL
            and r.status != ExecutionStatus.QUEUED.value
            and r.updated_at < stale_before
        ]
        rows.sort(key=lambda r: r.updated_at)
        return [r.id for r in rows[:limit]]

    async def mark_lost(self, *, generation_id: UUID, reason: str) -> bool:
        row = self.rows.get(generation_id)
        if row is None or row.status in TERMINAL or row.status == ExecutionStatus.QUEUED.value:
            return False
        row.status = ExecutionStatus.FAILED.value
        row.failure_reason = reason
        row.finished_at = datetime.now(UTC)
        return True

    async def cancel_queued(
        self, *, tenant_id: UUID, owner_user_id: UUID, generation_id: UUID
    ) -> CancelOutcome:
        row = self.rows.get(generation_id)
        if row is None or row.tenant_id != tenant_id or row.owner_user_id != owner_user_id:
            return CancelOutcome.NOT_FOUND
        if row.status != ExecutionStatus.QUEUED.value:
            return CancelOutcome.NOT_CANCELLABLE
        row.status = ExecutionStatus.CANCELLED.value
        row.finished_at = datetime.now(UTC)
        return CancelOutcome.CANCELLED


def _view(row: _Row) -> GenerationView:
    return GenerationView(
        id=row.id,
        status=row.status,
        prompt=row.spec.prompt,
        title=row.spec.title,
        aspect_ratio=row.spec.aspect_ratio,
        target_platform=row.spec.target_platform,
        width=row.spec.width,
        height=row.spec.height,
        fps=row.spec.fps,
        shot_count=row.shot_count,
        shots_accepted=row.shots_accepted,
        duration_seconds=None,
        failure_reason=row.failure_reason,
        promotable=row.promotable,
        created_at=row.created_at,
        started_at=None,
        finished_at=row.finished_at,
    )


class FakeLockManager(IDistributedLockManager):
    """Lease bookkeeping in memory, with the same steal-on-expiry contract as the SQL one."""

    def __init__(self, *, held: set[str] | None = None) -> None:
        self.leases: dict[str, Lease] = {}
        self.held_by_others = held or set()
        self.renewals: list[str] = []
        self.renew_fails = False

    async def acquire(self, *, key: str, owner: str, lease: timedelta) -> Lease | None:
        if key in self.held_by_others:
            return None
        now = datetime.now(UTC)
        existing = self.leases.get(key)
        if existing is not None and existing.lease_until > now:
            return None
        held = Lease(
            lock_key=key,
            owner=owner,
            lease_until=now + lease,
            heartbeat_at=now,
            acquired_at=now,
        )
        self.leases[key] = held
        return held

    async def renew(self, lease: Lease, *, lease_for: timedelta) -> Lease | None:
        self.renewals.append(lease.lock_key)
        if self.renew_fails:
            return None
        now = datetime.now(UTC)
        renewed = replace(lease, lease_until=now + lease_for, heartbeat_at=now)
        self.leases[lease.lock_key] = renewed
        return renewed

    async def release(self, lease: Lease) -> bool:
        return self.leases.pop(lease.lock_key, None) is not None

    async def reclaim_expired(self, *, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(UTC)
        expired = [k for k, v in self.leases.items() if v.lease_until < cutoff]
        for key in expired:
            del self.leases[key]
        return len(expired)


class FakeGenerationRunner(IGenerationRunner):
    """Records the requests it ran and returns a canned result (or raises)."""

    def __init__(
        self,
        *,
        status: GenerationStatus = GenerationStatus.SUCCEEDED,
        raises: Exception | None = None,
    ) -> None:
        self.requests: list[GenerateVideoRequest] = []
        self._status = status
        self._raises = raises

    async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        generation_id = request.generation_id or uuid4()
        return GenerateVideoResult(
            status=self._status,
            generation_id=generation_id,
            title=request.title or "untitled",
            provenance=GenerationProvenance(
                generation_id=generation_id,
                capability="image.generate",
                execution_mode=request.execution_mode.value,
                resolver_version="test",
            ),
        )
