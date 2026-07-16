"""``CreateRenderJob`` use case (Slice α7.1).

Contract (API_CONTRACT §3.2.5):

    POST /api/v1/projects/{project_id}/render-jobs
      body:  { pipeline?, pipeline_version?, queue?, priority?, idempotency_key? }
      → 201  { data: RenderJobPublic, meta }              (new job queued)
      → 200  { data: RenderJobPublic, meta }              (idempotent replay — existing job)
      → 404  { error: { code: NOT_FOUND, ... } }          (project missing / not yours)
      → 422  { error: { code: VALIDATION_FAILED, ... } }  (project has no timeline / bad body)
      → 401  { error: { code: UNAUTHENTICATED, ... } }    (via CurrentUserDep)

Creates the render job in ``queued`` (no worker yet — α8.x drives execution).
Ownership is derived through the project (the ``render_jobs`` row has no owner
columns), so step 1 is the project ownership gate (404-before-anything). The
timeline is resolved server-side (1:1 with the project, ADR-0038) — the client
renders "the project", not a chosen timeline; a project with no timeline is a
``422`` (nothing to render yet). Whether the job ultimately runs against the live
timeline (Draft) or a frozen ``ProjectVersion`` (Release) is the **worker's**
decision (α7.1 Q1 — deferred to α8.x), not persisted here.

**Idempotency (α7.1 Q4/D3.7).** When ``idempotency_key`` is supplied, a repeat
create with the same key for this project returns the **existing** job (the
router renders ``200``, not ``201``); the DB unique constraint
(``uq_render_jobs_project_id_idempotency_key``) is the race-safe backstop behind
the pre-check. The full idempotency ledger is a later slice.

On create, a ``RenderJobCreated`` event is written to the ``event_outbox`` in the
same transaction (blueprint §6 / D9). This use case never mutates another
aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.render._events import emit_render_job_created
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.render.render_job import RenderJob
from app.domain.render.render_status import RenderStatus

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CreateRenderJobResult:
    """The created (or idempotently-replayed) job plus whether it was newly made.

    ``created`` is ``True`` for a fresh insert (router → ``201``) and ``False``
    when an existing job was returned for a repeated ``idempotency_key`` (router
    → ``200``).
    """

    job: RenderJob
    created: bool


class CreateRenderJob:
    """Queue a render job for the caller's project (idempotent on ``idempotency_key``)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        pipeline: str,
        pipeline_version: str,
        queue: str,
        priority: int,
        idempotency_key: str | None = None,
        ip: str | None = None,
    ) -> CreateRenderJobResult:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "project not found",
                    details={"project_id": str(project_id)},
                )

            timeline = await self._uow.timeline.get_by_project(project_id)
            if timeline is None:
                # The project exists but has no timeline to render — the request
                # is well-formed but not fulfillable given current state (422,
                # not 404: the addressed project IS visible).
                raise ValidationFailedError(
                    "project has no timeline to render",
                    details={"project_id": str(project_id)},
                )

            # Idempotency pre-check (α7.1 Q4): a repeat key returns the existing
            # job rather than minting a duplicate.
            if idempotency_key is not None:
                existing = await self._uow.render_jobs.get_by_project_and_key(
                    project_id, idempotency_key
                )
                if existing is not None:
                    _LOGGER.info(
                        "render_job.create_idempotent_replay",
                        render_job_id=str(existing.id),
                        project_id=str(project_id),
                        idempotency_key=idempotency_key,
                        owner_user_id=str(owner_user_id),
                        ip=ip,
                    )
                    return CreateRenderJobResult(job=existing, created=False)

            try:
                job = await self._uow.render_jobs.add(
                    project_id=project_id,
                    timeline_id=timeline.id,
                    pipeline=pipeline,
                    pipeline_version=pipeline_version,
                    queue=queue,
                    priority=priority,
                    status=RenderStatus.QUEUED.value,
                    idempotency_key=idempotency_key,
                )
            except ConflictError:
                # Race: a concurrent request inserted the same
                # (project_id, idempotency_key) between our pre-check and insert.
                # Resolve idempotently by returning the winner (Q4). Only reachable
                # with a key (the unique constraint is on the pair).
                assert idempotency_key is not None
                winner = await self._uow.render_jobs.get_by_project_and_key(
                    project_id, idempotency_key
                )
                if winner is None:  # pragma: no cover — constraint says it exists
                    raise
                _LOGGER.info(
                    "render_job.create_idempotent_race",
                    render_job_id=str(winner.id),
                    project_id=str(project_id),
                    idempotency_key=idempotency_key,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return CreateRenderJobResult(job=winner, created=False)

            # Atomic state + event (D9): the RenderJobCreated row commits with the
            # job insert. No cross-aggregate mutation.
            await emit_render_job_created(self._uow, job, actor_user_id=owner_user_id)
            await self._uow.commit()

        _LOGGER.info(
            "render_job.created",
            render_job_id=str(job.id),
            project_id=str(project_id),
            timeline_id=str(job.timeline_id),
            pipeline=job.pipeline,
            pipeline_version=job.pipeline_version,
            queue=job.queue,
            priority=job.priority,
            idempotency_key=idempotency_key,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return CreateRenderJobResult(job=job, created=True)
