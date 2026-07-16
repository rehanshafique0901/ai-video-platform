"""``RenderJob`` domain entity — the Rendering & Export aggregate root (α7.1).

A **slim projection** of the ``render_jobs`` table (``schema.md`` §17 /
``models/jobs.py``). Frozen for value-semantics (mutations return new instances
via ``dataclasses.replace`` at the repository/use-case layer) — the same
discipline as :class:`app.domain.media.media_asset.MediaAsset` and
:class:`app.domain.projects.project.Project`.

Key modelling decisions (RENDER_JOB_AGGREGATE.md / ADR-0039 / α7.1 pre-flight):

* **Self-versioned aggregate** — ``render_jobs`` carries a ``version``
  (``VersionMixin``); the ``RenderJob`` fences on its **own** OCC token (the α6.2
  ``MediaAsset`` / α5a ``Project`` self-versioned pattern), NOT the borrowed
  timeline token of α6.3 children. Cancel is a version-fenced CAS → ``412``.
* **Ownership is derived through the project** (``project_id → projects.owner_user_id``);
  ``render_jobs`` carries no ``owner_user_id`` / ``tenant_id`` of its own.
* **Owns only orchestration metadata (D3.10).** ``timeline_id`` /
  ``workflow_run_id`` / ``output_media_asset_id`` are references (FKs), not owned
  state; the produced file lives on ``MediaAsset``.
* **No soft-delete** — ``render_jobs`` has no ``deleted_at``; a job is an
  operationally terminal audit record. "Removal" is the ``canceled`` status, not
  a delete.
* **Worker-owned fields are read-only in α7.1** — ``started_at`` / ``finished_at``
  / ``progress`` (beyond the ``'0.00'`` default) / ``error`` /
  ``output_media_asset_id`` are set by the render worker (α8.x); α7.1 never
  advances them.
* ``progress`` is decimal-as-text (``schema.md`` §17 / D5) — modelled as ``str``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RenderJob:
    """Render aggregate root — one row of the ``render_jobs`` table (slim view)."""

    id: UUID
    project_id: UUID
    timeline_id: UUID
    workflow_run_id: UUID | None
    pipeline: str
    pipeline_version: str
    queue: str
    priority: int
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    progress: str
    error: dict[str, Any] | None
    output_media_asset_id: UUID | None
    idempotency_key: str | None
    version: int
    created_at: datetime
    updated_at: datetime
