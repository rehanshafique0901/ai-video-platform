"""DTOs for ``/api/v1/projects/{project_id}/render-jobs/*`` endpoints (α7.1).

A **render job** is the request to render a project's timeline (ADR-0039):

* :class:`RenderJobCreateRequest` — ``POST`` body. Every field is optional with a
  sensible default: ``pipeline`` / ``pipeline_version`` describe the renderer
  (meaningful once multiple renderers exist — α7.1 Q2), ``queue`` / ``priority``
  are scheduling hints, ``idempotency_key`` enables idempotent replay (Q4).
  ``extra="forbid"`` turns any non-declared key into a ``422`` — the timeline is
  resolved server-side (1:1 with the project) and identity/lifecycle fields are
  server-owned, never client-supplied. There is **no** ``mode``/Release-vs-Draft
  field: that is the worker's concern (Q1, deferred to α8.x).
* :class:`RenderJobCancelRequest` — ``POST .../cancel`` body: the aggregate OCC
  ``version`` the client last observed (cancel is a version-fenced transition,
  Q3/D3.5).
* :class:`RenderJobPublic` — the response projection of
  :class:`app.domain.render.render_job.RenderJob`. Carries the ``version`` (the
  self-OCC token — unlike media, render jobs are version-controlled) and the
  worker-owned fields (``started_at`` / ``finished_at`` / ``progress`` / ``error``
  / ``output_media_asset_id``), which stay at their queued defaults in α7.1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Physical ``render_jobs.queue`` CHECK values (``models/jobs.py`` / schema.md §17).
RenderQueue = Literal["critical", "high", "normal", "low", "background"]

# Physical ``render_status`` ENUM (``enums.py`` / baseline 0001) — used to
# validate the ``?status=`` list filter.
RenderStatusLiteral = Literal["queued", "running", "succeeded", "failed", "canceled"]

_MAX_TEXT = 2_048
# Default renderer identity (α7.1 Q2): a single FFmpeg pipeline today; the version
# is a placeholder until the renderer surface is real (α8.x).
_DEFAULT_PIPELINE = "ffmpeg"
_DEFAULT_PIPELINE_VERSION = "0.0.0"


class RenderJobCreateRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/render-jobs body.

    All fields optional. ``pipeline`` / ``pipeline_version`` default to the single
    FFmpeg renderer; ``queue`` defaults to ``normal``; ``priority`` to ``0``.
    ``idempotency_key`` (when supplied) makes the create idempotent for this
    project (Q4).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    pipeline: str = Field(default=_DEFAULT_PIPELINE, min_length=1, max_length=_MAX_TEXT)
    pipeline_version: str = Field(
        default=_DEFAULT_PIPELINE_VERSION, min_length=1, max_length=_MAX_TEXT
    )
    queue: RenderQueue = "normal"
    priority: int = Field(default=0, ge=0, le=1000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class RenderJobCancelRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/render-jobs/{id}/cancel body.

    Carries only the aggregate OCC ``version`` the client last observed; cancel is
    a version-fenced transition (Q3/D3.5). ``extra="forbid"`` rejects stray keys.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class RenderJobPublic(BaseModel):
    """Public projection of :class:`app.domain.render.render_job.RenderJob`.

    Surfaces the self-OCC ``version`` (clients pass it back to cancel) and the
    worker-owned lifecycle fields (all at queued defaults in α7.1).
    """

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
