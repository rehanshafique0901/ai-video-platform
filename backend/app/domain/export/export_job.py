"""``ExportJob`` domain entity — the delivery-encoding aggregate root (α8.5a).

A **slim projection** of the ``export_jobs`` table (``schema.md`` §18 /
``models/jobs.py``). Frozen for value-semantics (mutations return new instances via
``dataclasses.replace`` at the repository layer) — the same discipline as
:class:`app.domain.render.render_job.RenderJob`.

Key modelling decisions (ADR-0030 / ADR-0039 / α8.5a pre-flight):

* **Self-versioned aggregate** — ``export_jobs`` carries a ``version``
  (``VersionMixin``); the job fences on its own OCC token (mirrors ``RenderJob``).
* **Ownership is derived through the render job → project.** ``export_jobs`` carries
  ``requested_by_user_id`` but no ``project_id`` / ``tenant_id``; ownership is resolved
  via ``render_job_id → render_jobs.project_id → projects`` (α8.5a Fork D).
* **The master is the render output.** ``render_job_id`` references the completed render
  whose ``output_media_asset_id`` is the export **source** (read-only, RC5). The export's
  own ``output_media_asset_id`` is the produced **delivery** artifact (W8.5.3).
* **No ``error`` / ``started_at`` columns** (unlike ``render_jobs``): a failed export
  records only ``status='failed'`` + ``finished_at``; the reason goes to logs + the
  ``ExportJobFailed`` event payload.
* **Idempotency** is the partial-unique index on ``(render_job_id, format, quality,
  orientation)`` where ``status IN ('queued','running','succeeded')`` (ADR-0030 W1.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ExportJob:
    """Export aggregate root — one row of the ``export_jobs`` table (slim view)."""

    id: UUID
    render_job_id: UUID
    requested_by_user_id: UUID
    format: str
    quality: str
    orientation: str
    status: str
    output_media_asset_id: UUID | None
    download_count: int
    last_downloaded_at: datetime | None
    file_size_bytes: int | None
    finished_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExportJobClaim:
    """A claimable export job + its resolved owning ``project_id`` (α8.5a poll ingress).

    ``export_jobs`` carries no ``project_id``; the worker-facing claim scan joins through
    ``render_jobs`` so the worker can settle each job via the project-scoped ports (the
    same shape ``ProcessRenderJob`` uses), without putting ``project_id`` on the entity.
    """

    export_job_id: UUID
    project_id: UUID
