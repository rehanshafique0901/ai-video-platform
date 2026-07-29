"""α9.7 — raw-SQL implementation of :class:`IGenerationJobStore` (ORM-less, ADR-0046 Q2).

Owns every write to the **ingress-owned** columns of `generations` (`tenant_id`,
`owner_user_id`, `idempotency_key`, `request`) and every owner-scoped read of the table. The
execution runtime writes the other half of the row and never touches these columns
(pre-flight GEN-1).

Same shape as :class:`SqlExecutionRuntimeStore` / :class:`GenerationReader`: an
``async_sessionmaker`` and short, self-contained transactions, because generation is far too
long-running for one transaction to span a run.

Two SQL properties are load-bearing rather than incidental:

* **Every owner-scoped statement carries `owner_user_id IS NOT NULL` implicitly** by comparing
  against a non-null bind. Legacy ownerless rows can therefore never match, which is exactly
  ADR-0052's "invisible, never inferred" ruling expressed in the query rather than in a comment.
* **Every state change is a CAS.** Claiming is `queued → planning`, cancelling is
  `queued → cancelled`, reaping is `non-terminal → failed`. Concurrency is settled by the
  database, never by read-then-write in Python.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.generation_job_store import (
    CancelOutcome,
    ClaimedGeneration,
    CreatedGeneration,
    GenerationView,
    IGenerationJobStore,
)
from app.application.pagination import Cursor
from app.application.use_cases.generation.request_codec import (
    GenerationRequestSpec,
    decode_spec,
    encode_spec,
)
from app.domain.generation.execution_state import ExecutionStatus

# Statuses a generation can never leave. Claim/cancel/reap all refuse to touch them.
_TERMINAL = (
    ExecutionStatus.COMPLETED.value,
    ExecutionStatus.FAILED.value,
    ExecutionStatus.CANCELLED.value,
)

_INSERT_SQL = text(
    """
    INSERT INTO generations (
        id, tenant_id, owner_user_id, idempotency_key, request,
        status, prompt, title, execution_mode, seed,
        aspect_ratio, target_platform, width, height, fps
    ) VALUES (
        CAST(:id AS uuid), CAST(:tenant_id AS uuid), CAST(:owner_user_id AS uuid),
        :idempotency_key, CAST(:request AS jsonb),
        CAST(:status AS generation_status), :prompt, :title, :execution_mode, :seed,
        :aspect_ratio, :target_platform, :width, :height, :fps
    )
    RETURNING id, created_at
    """
)

# The curated projection (ADR-0052 D3). `shots_accepted` is a correlated count rather than a
# GROUP BY so the shape stays identical between the single-row read and the list page.
_VIEW_COLUMNS = """
        g.id, g.status, g.prompt, g.title,
        g.aspect_ratio, g.target_platform, g.width, g.height, g.fps,
        g.shot_count, g.duration_seconds, g.failure_reason,
        g.created_at, g.started_at, g.finished_at,
        (g.status = 'completed' AND g.final_video_asset_id IS NOT NULL) AS promotable,
        (SELECT count(*) FROM generation_shots s
          WHERE s.generation_id = g.id AND s.accepted) AS shots_accepted
"""

_GET_OWNED_SQL = text(
    f"""
    SELECT {_VIEW_COLUMNS}
    FROM generations g
    WHERE g.id = CAST(:id AS uuid)
      AND g.tenant_id = CAST(:tenant_id AS uuid)
      AND g.owner_user_id = CAST(:owner_user_id AS uuid)
    """
)

_GET_BY_KEY_SQL = text(
    f"""
    SELECT {_VIEW_COLUMNS}
    FROM generations g
    WHERE g.owner_user_id = CAST(:owner_user_id AS uuid)
      AND g.idempotency_key = :idempotency_key
    """
)

# Keyset page. The (created_at, id) row-value comparison is the platform's total order
# (app.application.pagination) and matches ix_generations_owner_created exactly.
#
# Every optional bind is explicitly cast. PostgreSQL cannot infer a type for a bare parameter
# in `$1 IS NULL`, so an uncast optional filter fails at *prepare* time — before any row is
# read — rather than merely returning the wrong rows.
_LIST_OWNED_SQL = text(
    f"""
    SELECT {_VIEW_COLUMNS}
    FROM generations g
    WHERE g.tenant_id = CAST(:tenant_id AS uuid)
      AND g.owner_user_id = CAST(:owner_user_id AS uuid)
      AND (
            CAST(:status AS text) IS NULL
            OR g.status = CAST(:status AS generation_status)
          )
      AND (
            CAST(:cursor_created_at AS timestamptz) IS NULL
            OR (g.created_at, g.id)
               < (CAST(:cursor_created_at AS timestamptz), CAST(:cursor_id AS uuid))
          )
    ORDER BY g.created_at DESC, g.id DESC
    LIMIT :limit
    """
)

_LIST_CLAIMABLE_SQL = text(
    """
    SELECT id FROM generations
    WHERE status = 'queued'
    ORDER BY created_at ASC, id ASC
    LIMIT :limit
    """
)

# The claim CAS. Returning `request` in the same statement means a claim and its payload are
# read atomically — no window in which a claimed row's request could be re-read differently.
_CLAIM_SQL = text(
    """
    UPDATE generations
       SET status = 'planning', updated_at = now()
     WHERE id = CAST(:id AS uuid) AND status = 'queued'
    RETURNING id, request
    """
)

_LIST_REAPABLE_SQL = text(
    """
    SELECT id FROM generations
    WHERE status NOT IN ('queued', 'completed', 'failed', 'cancelled')
      AND updated_at < :stale_before
    ORDER BY updated_at ASC
    LIMIT :limit
    """
)

_MARK_LOST_SQL = text(
    """
    UPDATE generations
       SET status = 'failed', failure_reason = :reason,
           finished_at = now(), updated_at = now()
     WHERE id = CAST(:id AS uuid)
       AND status NOT IN ('queued', 'completed', 'failed', 'cancelled')
    RETURNING id
    """
)

_CANCEL_QUEUED_SQL = text(
    """
    UPDATE generations
       SET status = 'cancelled', finished_at = now(), updated_at = now()
     WHERE id = CAST(:id AS uuid)
       AND tenant_id = CAST(:tenant_id AS uuid)
       AND owner_user_id = CAST(:owner_user_id AS uuid)
       AND status = 'queued'
    RETURNING id
    """
)

_EXISTS_OWNED_SQL = text(
    """
    SELECT 1 FROM generations
    WHERE id = CAST(:id AS uuid)
      AND tenant_id = CAST(:tenant_id AS uuid)
      AND owner_user_id = CAST(:owner_user_id AS uuid)
    """
)


class SqlGenerationJobStore(IGenerationJobStore):
    """Ingress-owned raw-SQL persistence for `generations`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
            existing = await self._get_by_key(
                owner_user_id=owner_user_id, idempotency_key=idempotency_key
            )
            if existing is not None:
                return CreatedGeneration(view=existing, created=False)

        params = {
            "id": str(generation_id),
            "tenant_id": str(tenant_id),
            "owner_user_id": str(owner_user_id),
            "idempotency_key": idempotency_key,
            "request": json.dumps(encode_spec(spec)),
            "status": ExecutionStatus.QUEUED.value,
            "prompt": spec.prompt,
            "title": spec.title,
            "execution_mode": spec.execution_mode,
            "seed": spec.seed,
            "aspect_ratio": spec.aspect_ratio,
            "target_platform": spec.target_platform,
            "width": spec.width,
            "height": spec.height,
            "fps": spec.fps,
        }
        try:
            async with self._session_factory() as session:
                inserted = (await session.execute(_INSERT_SQL, params)).mappings().one()
                await session.commit()
        except IntegrityError:
            # A concurrent create with the same key won the unique index. The constraint
            # decided the race (ADR-0048); we simply read back the winner.
            if idempotency_key is None:
                raise
            winner = await self._get_by_key(
                owner_user_id=owner_user_id, idempotency_key=idempotency_key
            )
            if winner is None:  # pragma: no cover - only if the conflict was something else
                raise
            return CreatedGeneration(view=winner, created=False)

        # Built from the INSERT's own RETURNING rather than re-read: a freshly queued
        # generation has no shots, no duration and nothing promotable, so every other field
        # of the projection is known here without a second round trip.
        return CreatedGeneration(
            view=GenerationView(
                id=inserted["id"],
                status=ExecutionStatus.QUEUED.value,
                prompt=spec.prompt,
                title=spec.title,
                aspect_ratio=spec.aspect_ratio,
                target_platform=spec.target_platform,
                width=spec.width,
                height=spec.height,
                fps=spec.fps,
                shot_count=None,
                shots_accepted=0,
                duration_seconds=None,
                failure_reason=None,
                promotable=False,
                created_at=inserted["created_at"],
                started_at=None,
                finished_at=None,
            ),
            created=True,
        )

    # ---- owner-scoped reads ----------------------------------------------- #

    async def get_owned(
        self, *, tenant_id: UUID, owner_user_id: UUID, generation_id: UUID
    ) -> GenerationView | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        _GET_OWNED_SQL,
                        {
                            "id": str(generation_id),
                            "tenant_id": str(tenant_id),
                            "owner_user_id": str(owner_user_id),
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _to_view(row) if row is not None else None

    async def list_owned(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        cursor: Cursor | None = None,
        status: str | None = None,
    ) -> list[GenerationView]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        _LIST_OWNED_SQL,
                        {
                            "tenant_id": str(tenant_id),
                            "owner_user_id": str(owner_user_id),
                            "status": status,
                            "cursor_created_at": cursor.created_at if cursor else None,
                            "cursor_id": str(cursor.id) if cursor else None,
                            "limit": limit,
                        },
                    )
                )
                .mappings()
                .all()
            )
        return [_to_view(r) for r in rows]

    # ---- worker ----------------------------------------------------------- #

    async def list_claimable(self, *, limit: int) -> list[UUID]:
        async with self._session_factory() as session:
            rows = (await session.execute(_LIST_CLAIMABLE_SQL, {"limit": limit})).all()
        return [r[0] for r in rows]

    async def claim(self, *, generation_id: UUID) -> ClaimedGeneration | None:
        async with self._session_factory() as session:
            row = (
                (await session.execute(_CLAIM_SQL, {"id": str(generation_id)}))
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            await session.commit()
        return ClaimedGeneration(generation_id=row["id"], spec=decode_spec(dict(row["request"])))

    async def list_reapable(self, *, stale_before: datetime, limit: int) -> list[UUID]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    _LIST_REAPABLE_SQL, {"stale_before": stale_before, "limit": limit}
                )
            ).all()
        return [r[0] for r in rows]

    async def mark_lost(self, *, generation_id: UUID, reason: str) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.execute(_MARK_LOST_SQL, {"id": str(generation_id), "reason": reason})
            ).one_or_none()
            await session.commit()
        return row is not None

    # ---- owner-scoped cancel ---------------------------------------------- #

    async def cancel_queued(
        self, *, tenant_id: UUID, owner_user_id: UUID, generation_id: UUID
    ) -> CancelOutcome:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _CANCEL_QUEUED_SQL,
                    {
                        "id": str(generation_id),
                        "tenant_id": str(tenant_id),
                        "owner_user_id": str(owner_user_id),
                    },
                )
            ).one_or_none()
            if row is not None:
                await session.commit()
                return CancelOutcome.CANCELLED
            # The CAS matched nothing: either the row is not the caller's (indistinguishable
            # from absent — uniform 404) or it has already been claimed (409, PF4).
            owned = (
                await session.execute(
                    _EXISTS_OWNED_SQL,
                    {
                        "id": str(generation_id),
                        "tenant_id": str(tenant_id),
                        "owner_user_id": str(owner_user_id),
                    },
                )
            ).one_or_none()
        return CancelOutcome.NOT_CANCELLABLE if owned is not None else CancelOutcome.NOT_FOUND

    # ---- internals -------------------------------------------------------- #

    async def _get_by_key(
        self, *, owner_user_id: UUID, idempotency_key: str
    ) -> GenerationView | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        _GET_BY_KEY_SQL,
                        {
                            "owner_user_id": str(owner_user_id),
                            "idempotency_key": idempotency_key,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _to_view(row) if row is not None else None


def _to_view(row: RowMapping) -> GenerationView:
    duration = row["duration_seconds"]
    return GenerationView(
        id=row["id"],
        status=str(row["status"]),
        prompt=row["prompt"],
        title=row["title"],
        aspect_ratio=row["aspect_ratio"],
        target_platform=row["target_platform"],
        width=row["width"],
        height=row["height"],
        fps=row["fps"],
        shot_count=row["shot_count"],
        shots_accepted=int(row["shots_accepted"] or 0),
        duration_seconds=float(duration) if duration is not None else None,
        failure_reason=row["failure_reason"],
        promotable=bool(row["promotable"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


__all__ = ["SqlGenerationJobStore"]
