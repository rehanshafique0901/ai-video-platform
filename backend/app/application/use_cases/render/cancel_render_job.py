"""``CancelRenderJob`` use case (Slice α7.1).

Contract (API_CONTRACT §3.2.5):

    POST /api/v1/projects/{project_id}/render-jobs/{render_job_id}/cancel
      body:  { version }
      → 200  { data: RenderJobPublic (status=canceled), meta }
      → 404  { error: { code: NOT_FOUND, ... } }          (project/job missing / not yours)
      → 409  { error: { code: CONFLICT, ... } }           (already succeeded/failed)
      → 412  { error: { code: VERSION_CONFLICT, ... } }   (stale version, still cancelable)
      → 422  { error: { code: VALIDATION_FAILED, ... } }  (missing version body)
      → 401  { error: { code: UNAUTHENTICATED, ... } }    (via CurrentUserDep)

Cancel is a **version-fenced status transition** (α7.1 Q3/D3.5–D3.6), a POST verb
(it changes state), returning the canceled job (``200``). The state machine:

    queued  ─▶ canceled   ✅
    running ─▶ canceled   ✅ (best-effort; the worker observes the flag in α8.x)
    canceled ─▶ canceled  ⇒ 200 no-op (idempotent)
    succeeded/failed ─▶ cancel ⇒ 409 (completed work is not cancelable)

Control flow mirrors the α5b/α6.3 fetch-then-fence (404-before-412): project
gate → job visibility (404) → terminal-state classification → version-fenced CAS.
The CAS carries a ``status IN ('queued','running')`` predicate so a worker that
completes the job between the read and the write is not silently overwritten —
the terminal guard is race-safe at the DB, and a ``None`` CAS result is
re-classified (canceled → 200 no-op; terminal → 409; else stale → 412).

On a real cancel a ``RenderJobCanceled`` event is written to the ``event_outbox``
in the same transaction (blueprint §6 / D9).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.render._events import emit_render_job_canceled
from app.core.errors import ConflictError, NotFoundError, VersionConflictError
from app.domain.render.render_job import RenderJob
from app.domain.render.render_status import RenderStatus

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CancelRenderJobResult:
    """The (now-)canceled job plus whether this call actually changed state.

    ``canceled`` is ``True`` when this call transitioned the job to ``canceled``
    (a ``RenderJobCanceled`` event was emitted); ``False`` for an idempotent
    re-cancel of an already-``canceled`` job (no event, no state change). Both
    render ``200``.
    """

    job: RenderJob
    canceled: bool


class CancelRenderJob:
    """Version-fenced cancel of the caller's project render job."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        project_id: UUID,
        render_job_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        ip: str | None = None,
    ) -> CancelRenderJobResult:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
            )
            if project is None:
                raise NotFoundError(
                    "render job not found",
                    details={"render_job_id": str(render_job_id)},
                )

            job = await self._uow.render_jobs.get_owned(project_id, render_job_id)
            if job is None:
                raise NotFoundError(
                    "render job not found",
                    details={"render_job_id": str(render_job_id)},
                )

            status = RenderStatus(job.status)

            # Idempotent re-cancel: already in the desired terminal state → 200
            # no-op, no version fence (the outcome the client asked for already
            # holds), no event.
            if status is RenderStatus.CANCELED:
                _LOGGER.info(
                    "render_job.cancel_noop",
                    render_job_id=str(job.id),
                    project_id=str(project_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return CancelRenderJobResult(job=job, canceled=False)

            # Completed work is not cancelable → 409.
            if not status.is_cancelable:
                _LOGGER.warning(
                    "render_job.cancel_rejected",
                    reason="terminal_state",
                    render_job_id=str(job.id),
                    project_id=str(project_id),
                    status=job.status,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise ConflictError(
                    "render job is already complete and cannot be canceled",
                    details={"render_job_id": str(render_job_id), "status": job.status},
                )

            # Cancelable (queued/running): fence on the observed version → 412.
            if job.version != expected_version:
                _LOGGER.warning(
                    "render_job.cancel_rejected",
                    reason="version_mismatch",
                    render_job_id=str(job.id),
                    project_id=str(project_id),
                    expected_version=expected_version,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            canceled = await self._uow.render_jobs.cancel(
                project_id, render_job_id, expected_version
            )
            if canceled is None:
                # The CAS lost — re-classify against the current row (a worker may
                # have raced the job to a terminal/canceled state, or a concurrent
                # writer bumped the version).
                current = await self._uow.render_jobs.get_owned(project_id, render_job_id)
                if current is not None and RenderStatus(current.status) is RenderStatus.CANCELED:
                    return CancelRenderJobResult(job=current, canceled=False)
                if current is not None and not RenderStatus(current.status).is_cancelable:
                    raise ConflictError(
                        "render job is already complete and cannot be canceled",
                        details={
                            "render_job_id": str(render_job_id),
                            "status": current.status,
                        },
                    )
                _LOGGER.warning(
                    "render_job.cancel_rejected",
                    reason="version_mismatch_cas",
                    render_job_id=str(render_job_id),
                    project_id=str(project_id),
                    expected_version=expected_version,
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            # Atomic state + event (D9).
            await emit_render_job_canceled(self._uow, canceled, actor_user_id=owner_user_id)
            await self._uow.commit()

        _LOGGER.info(
            "render_job.canceled",
            render_job_id=str(canceled.id),
            project_id=str(project_id),
            previous_version=expected_version,
            new_version=canceled.version,
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return CancelRenderJobResult(job=canceled, canceled=True)
