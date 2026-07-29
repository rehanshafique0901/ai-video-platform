"""α9.7 — the owner-scoped generation read model and cancel action (ADR-0052 D3/D5).

Polling is the v1 progress contract: a generation runs for minutes, so sub-second update
latency has no product value, and a `GET` that returns a value is deterministic in CI in a way
a stream is not.

**Owner-scoped reads are the sole supported read model.** A generation the caller does not own
is reported exactly like one that does not exist — a `404` — so an id cannot be probed. Legacy
ownerless rows (written before this slice) match no owner predicate and are therefore invisible
here; that is ADR-0052's migration philosophy, not an oversight.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.generation_job_store import (
    CancelOutcome,
    GenerationView,
    IGenerationJobStore,
)
from app.application.pagination import Cursor, Page, decode_cursor, encode_cursor
from app.core.errors import ConflictError, NotFoundError


class GetGeneration:
    """Fetch one of the caller's generations."""

    def __init__(self, store: IGenerationJobStore) -> None:
        self._store = store

    async def execute(
        self, *, tenant_id: UUID, owner_user_id: UUID, generation_id: UUID
    ) -> GenerationView:
        view = await self._store.get_owned(
            tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=generation_id
        )
        if view is None:
            raise NotFoundError(
                "generation not found", details={"generation_id": str(generation_id)}
            )
        return view


class ListGenerations:
    """Keyset page of the caller's generations, newest first."""

    def __init__(self, store: IGenerationJobStore) -> None:
        self._store = store

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        cursor_token: str | None = None,
        status: str | None = None,
    ) -> Page[GenerationView]:
        cursor: Cursor | None = decode_cursor(cursor_token) if cursor_token else None
        # Over-fetch by one: the presence of an (N+1)th row is what proves another page
        # exists, without a second COUNT query.
        rows = await self._store.list_owned(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            limit=limit + 1,
            cursor=cursor,
            status=status,
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(Cursor(created_at=items[-1].created_at, id=items[-1].id))
            if has_more and items
            else None
        )
        return Page(items=items, next_cursor=next_cursor)


class CancelGeneration:
    """Cancel one of the caller's generations while it is still queued (pre-flight PF4).

    Mid-run cancellation would require the pipeline to poll a flag between shots — a change to
    the execution plane this slice otherwise leaves untouched — so a claimed generation is a
    ``409`` rather than a lie. Cancelling before a worker claims it is fully deterministic and
    guarantees no provider spend.
    """

    def __init__(self, store: IGenerationJobStore) -> None:
        self._store = store

    async def execute(
        self, *, tenant_id: UUID, owner_user_id: UUID, generation_id: UUID
    ) -> GenerationView:
        outcome = await self._store.cancel_queued(
            tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=generation_id
        )
        if outcome is CancelOutcome.NOT_FOUND:
            raise NotFoundError(
                "generation not found", details={"generation_id": str(generation_id)}
            )
        if outcome is CancelOutcome.NOT_CANCELLABLE:
            raise ConflictError(
                "generation is no longer queued and cannot be cancelled",
                details={"generation_id": str(generation_id)},
            )
        view = await self._store.get_owned(
            tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=generation_id
        )
        if view is None:  # pragma: no cover - cancelled row cannot vanish mid-request
            raise NotFoundError(
                "generation not found", details={"generation_id": str(generation_id)}
            )
        return view
