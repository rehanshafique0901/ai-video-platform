"""``PublishJob`` domain entity — the publish-execution aggregate root (α8.6b).

A **slim projection** of the ``publish_jobs`` table, frozen for value-semantics — the same
discipline as :class:`app.domain.export.export_job.ExportJob`, which this faithfully adapts
(DQ8: self-versioned OCC; transitions live at the repository layer as status-fenced CAS,
not on the entity).

Key modelling decisions (α8.6b pre-flight §2–§3):

* **Direct ownership** — ``tenant_id`` + ``requested_by_user_id`` are on the row (the
  ``MediaAsset`` / user-initiated convention), unlike ``export_jobs`` whose ownership is
  derived through the render job. PUB-2: a job always carries explicit user intent.
* **Explicit ``project_id`` (DQ1)** — resolved once at creation from the export→render
  chain and persisted, so the ``project_publish:<project_id>`` serialisation lock is always
  well-defined (``MediaAsset.project_id`` is nullable and cannot be relied on).
* **Source is the export delivery artifact (PUB-1)** — ``source_media_asset_id`` is the
  ``export_jobs.output_media_asset_id``; ``source_export_job_id`` records provenance.
* **Credentials are never here (PUB-5)** — only ``social_account_id`` + ``platform``; the
  worker fetches an ``AuthorizedContext`` from the credential service at run time.
* **Scheduling + retries are fields, not states** — ``scheduled_at`` gates claiming;
  ``attempt`` / ``max_attempts`` bound retries (DQ6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.domain.publishing.content_package import ContentPackage


@dataclass(frozen=True, slots=True)
class PublishJob:
    """Publish aggregate root — one row of the ``publish_jobs`` table (slim view)."""

    id: UUID
    tenant_id: UUID
    requested_by_user_id: UUID
    project_id: UUID
    source_export_job_id: UUID
    source_media_asset_id: UUID
    social_account_id: UUID
    platform: str
    status: str
    scheduled_at: datetime | None
    attempt: int
    max_attempts: int
    content_package: ContentPackage
    platform_post_id: str | None
    platform_post_url: str | None
    error: dict[str, Any] | None
    published_at: datetime | None
    finished_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PublishSource:
    """The owner-verified export delivery artifact a publish job will consume (PUB-1).

    Resolved once at creation by the publishing repository, which reads the finished
    ``export_jobs`` row and joins ``render_jobs → projects`` for owner scoping (α8.6b
    consumes export deliveries; it never re-derives the resolution lazily, DQ1). Carries the
    owning ``project_id`` (for the persisted ``publish_jobs.project_id`` + the project lock),
    the delivery ``source_media_asset_id`` (``export_jobs.output_media_asset_id``), and the
    export ``status`` so the use case can require a ``succeeded`` export with an artifact.
    """

    export_job_id: UUID
    project_id: UUID
    source_media_asset_id: UUID | None
    export_status: str


@dataclass(frozen=True, slots=True)
class PublishJobClaim:
    """A claimable publish job + its owning ``project_id`` (the poll ingress).

    Unlike :class:`app.domain.export.export_job.ExportJobClaim`, ``project_id`` is a real
    column on ``publish_jobs`` (DQ1), so the claim scan needs no join to resolve it — the
    worker uses it directly for the ``project_publish:<project_id>`` lock.
    """

    publish_job_id: UUID
    project_id: UUID


__all__ = ["PublishJob", "PublishJobClaim", "PublishSource"]
