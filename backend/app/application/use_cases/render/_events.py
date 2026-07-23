"""Domain-event emitters for the RenderJob aggregate (Slice α7.1).

The one place that knows the *shape* of RenderJob outbox events, so
``CreateRenderJob`` / ``CancelRenderJob`` stay focused on control flow and the
event schema lives in a single reviewable spot. Events are written to the
``event_outbox`` **inside the caller's UnitOfWork transaction** (before
``commit()``), so the aggregate mutation and its event commit atomically — the
transactional-outbox guarantee (blueprint §6 / D9). α7.1 produces the rows;
publication is a later slice.

``occurred_at`` is the wall-clock instant the domain event happened. These use
cases follow the α5/α6 convention of not injecting a clock (only auth does), so
the emitter reads ``datetime.now(UTC)`` directly — the outbox row's ``created_at``
is DB-owned and effectively coincident.

Event versioning: ``event_version`` starts at ``"1.0"``. Any breaking change to a
payload shape below MUST bump it (per the blueprint's event-shape discipline).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.domain.render.render_job import RenderJob

AGGREGATE_TYPE = "render_job"
EVENT_RENDER_JOB_CREATED = "RenderJobCreated"
EVENT_RENDER_JOB_CANCELED = "RenderJobCanceled"
EVENT_RENDER_JOB_SUCCEEDED = "RenderJobSucceeded"
EVENT_RENDER_JOB_FAILED = "RenderJobFailed"


def _base_payload(job: RenderJob) -> dict[str, object]:
    """The common RenderJob event body (identity + orchestration metadata only).

    Deliberately carries orchestration fields only (D3.10): no rendered-file or
    timeline-edit state — a consumer that needs those resolves them from the
    referenced aggregates.
    """
    return {
        "render_job_id": str(job.id),
        "project_id": str(job.project_id),
        "timeline_id": str(job.timeline_id),
        "pipeline": job.pipeline,
        "pipeline_version": job.pipeline_version,
        "queue": job.queue,
        "priority": job.priority,
        "status": job.status,
        "version": job.version,
    }


async def emit_render_job_created(uow: IUnitOfWork, job: RenderJob, *, actor_user_id: UUID) -> None:
    """Append a ``RenderJobCreated`` event for a freshly-queued job."""
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_RENDER_JOB_CREATED,
        payload=_base_payload(job),
        occurred_at=datetime.now(UTC),
        metadata={"actor_user_id": str(actor_user_id)},
    )


async def emit_render_job_canceled(
    uow: IUnitOfWork, job: RenderJob, *, actor_user_id: UUID
) -> None:
    """Append a ``RenderJobCanceled`` event for a just-canceled job."""
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_RENDER_JOB_CANCELED,
        payload=_base_payload(job),
        occurred_at=datetime.now(UTC),
        metadata={"actor_user_id": str(actor_user_id)},
    )


async def emit_render_job_succeeded(uow: IUnitOfWork, job: RenderJob) -> None:
    """Append a ``RenderJobSucceeded`` event for a just-rendered job (α8.4b).

    Emitted by the render worker (system actor — no user), inside the same UoW as
    the ``running`` → ``succeeded`` CAS so the settle + event commit atomically.
    Carries only orchestration identity + the produced ``output_media_asset_id`` —
    no rendered-file bytes, no provider/timeline-edit state (W8.4b.2).
    """
    payload = _base_payload(job)
    payload["output_media_asset_id"] = (
        str(job.output_media_asset_id) if job.output_media_asset_id is not None else None
    )
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_RENDER_JOB_SUCCEEDED,
        payload=payload,
        occurred_at=datetime.now(UTC),
        metadata={"actor": "render_worker"},
    )


async def emit_render_job_failed(
    uow: IUnitOfWork, job: RenderJob, *, error: dict[str, object]
) -> None:
    """Append a ``RenderJobFailed`` event for a job the worker could not render.

    Emitted inside the same UoW as the ``running`` → ``failed`` CAS. ``error`` is a
    neutral dict (a ``code``/``message``) — no provider or orchestration internals
    leak into the render path (W8.4b.2).
    """
    payload = _base_payload(job)
    payload["error"] = error
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_RENDER_JOB_FAILED,
        payload=payload,
        occurred_at=datetime.now(UTC),
        metadata={"actor": "render_worker"},
    )
