"""Domain-event emitters for the PublishJob aggregate (Slice α8.6b).

The one place that knows the *shape* of PublishJob outbox events, so the create/worker use
cases stay focused on control flow. Events are written to the ``event_outbox`` **inside the
caller's UnitOfWork transaction** (before ``commit()``), so the aggregate mutation and its
event commit atomically — the transactional-outbox guarantee. Mirrors
``app.application.use_cases.export._events``.

PascalCase ``event_type`` (DQ4), consistent with the existing event model. Payloads carry
only publish-orchestration identity + the platform post identity on success — **never** a
credential, bearer, provider URL, or content bytes (PUB-8 / ADR-0047 C8).

DQ7 (deferred): α8.6b emits terminal events only. There is **no** notification projection in
this slice; a downstream ``publish.*`` consumer is a follow-up (PUB-8: events are fan-out).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.domain.publishing.publish_job import PublishJob

AGGREGATE_TYPE = "publish_job"
EVENT_PUBLISH_JOB_CREATED = "PublishJobCreated"
EVENT_PUBLISH_JOB_SUCCEEDED = "PublishJobSucceeded"
EVENT_PUBLISH_JOB_FAILED = "PublishJobFailed"


def _base_payload(job: PublishJob) -> dict[str, Any]:
    """The common PublishJob event body (identity + destination only — no secrets)."""
    return {
        "publish_job_id": str(job.id),
        "project_id": str(job.project_id),
        "requested_by_user_id": str(job.requested_by_user_id),
        "social_account_id": str(job.social_account_id),
        "platform": job.platform,
        "source_export_job_id": str(job.source_export_job_id),
        "source_media_asset_id": str(job.source_media_asset_id),
        "status": job.status,
        "version": job.version,
    }


async def emit_publish_job_created(
    uow: IUnitOfWork, job: PublishJob, *, actor_user_id: UUID
) -> None:
    """Append a ``PublishJobCreated`` event for a freshly-queued publish."""
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_PUBLISH_JOB_CREATED,
        payload=_base_payload(job),
        occurred_at=datetime.now(UTC),
        metadata={"actor_user_id": str(actor_user_id)},
    )


async def emit_publish_job_succeeded(uow: IUnitOfWork, job: PublishJob) -> None:
    """Append a ``PublishJobSucceeded`` event for a just-published artifact.

    Emitted by the publish worker (system actor), inside the same UoW as the ``running`` →
    ``succeeded`` CAS. Carries the platform post identity only — no bearer, no bytes (C8).
    """
    payload = _base_payload(job)
    payload["platform_post_id"] = job.platform_post_id
    payload["platform_post_url"] = job.platform_post_url
    payload["published_at"] = job.published_at.isoformat() if job.published_at else None
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_PUBLISH_JOB_SUCCEEDED,
        payload=payload,
        occurred_at=datetime.now(UTC),
        metadata={"actor": "publish_worker"},
    )


async def emit_publish_job_failed(
    uow: IUnitOfWork, job: PublishJob, *, error: dict[str, Any]
) -> None:
    """Append a ``PublishJobFailed`` event for a job the worker could not publish.

    Emitted inside the same UoW as the ``running`` → ``failed`` CAS. ``error`` is a neutral
    dict (a ``code``/``message``) — no credential or platform internals leak (PUB-8 / C8).
    """
    payload = _base_payload(job)
    payload["error"] = error
    await uow.outbox.add(
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job.id,
        event_type=EVENT_PUBLISH_JOB_FAILED,
        payload=payload,
        occurred_at=datetime.now(UTC),
        metadata={"actor": "publish_worker"},
    )
