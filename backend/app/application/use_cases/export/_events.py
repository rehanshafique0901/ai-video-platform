"""Domain-event emitters for the ExportJob aggregate (Slice α8.5a).

The one place that knows the *shape* of ExportJob outbox events, so the create/worker use
cases stay focused on control flow. Events are written to the ``event_outbox`` **inside the
caller's UnitOfWork transaction** (before ``commit()``), so the aggregate mutation and its
event commit atomically — the transactional-outbox guarantee (blueprint §6 / D9). Mirrors
``app.application.use_cases.render._events``.

Export events carry only delivery-orchestration identity (W8.5.2): the export/render ids, the
requested encoding, and — on success — the produced delivery ``output_media_asset_id`` +
``file_size_bytes``. No Timeline, provider, or workflow state ever appears here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.domain.export.export_job import ExportJob

AGGREGATE_TYPE = "export_job"
EVENT_EXPORT_JOB_CREATED = "ExportJobCreated"
EVENT_EXPORT_JOB_SUCCEEDED = "ExportJobSucceeded"
EVENT_EXPORT_JOB_FAILED = "ExportJobFailed"


def _base_payload(job: ExportJob) -> dict[str, object]:
    """The common ExportJob event body (identity + requested encoding only)."""
    return {
        "export_job_id": str(job.id),
        "render_job_id": str(job.render_job_id),
        "requested_by_user_id": str(job.requested_by_user_id),
        "format": job.format,
        "quality": job.quality,
        "orientation": job.orientation,
        "status": job.status,
        "version": job.version,
    }


async def emit_export_job_created(uow: IUnitOfWork, job: ExportJob, *, actor_user_id: UUID) -> None:
    """Append an ``ExportJobCreated`` event for a freshly-queued export."""
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_EXPORT_JOB_CREATED,
        payload=_base_payload(job),
        occurred_at=datetime.now(UTC),
        metadata={"actor_user_id": str(actor_user_id)},
    )


async def emit_export_job_succeeded(uow: IUnitOfWork, job: ExportJob) -> None:
    """Append an ``ExportJobSucceeded`` event for a just-exported delivery artifact.

    Emitted by the export worker (system actor), inside the same UoW as the ``running`` →
    ``succeeded`` CAS. Carries the produced ``output_media_asset_id`` + ``file_size_bytes``
    only — no bytes, no provider/timeline state (W8.5.2).
    """
    payload = _base_payload(job)
    payload["output_media_asset_id"] = (
        str(job.output_media_asset_id) if job.output_media_asset_id is not None else None
    )
    payload["file_size_bytes"] = job.file_size_bytes
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_EXPORT_JOB_SUCCEEDED,
        payload=payload,
        occurred_at=datetime.now(UTC),
        metadata={"actor": "export_worker"},
    )


async def emit_export_job_failed(
    uow: IUnitOfWork, job: ExportJob, *, error: dict[str, object]
) -> None:
    """Append an ``ExportJobFailed`` event for a job the worker could not export.

    Emitted inside the same UoW as the ``running`` → ``failed`` CAS. ``error`` is a neutral
    dict (a ``code``/``message``) — no provider or orchestration internals leak (W8.5.2).
    """
    payload = _base_payload(job)
    payload["error"] = error
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_EXPORT_JOB_FAILED,
        payload=payload,
        occurred_at=datetime.now(UTC),
        metadata={"actor": "export_worker"},
    )
