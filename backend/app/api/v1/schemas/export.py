"""DTOs for ``/api/v1/projects/{project_id}/render-jobs/{render_job_id}/exports/*`` (α8.5a).

An **export job** is a user's request to transcode a completed render's master
``MediaAsset`` into one delivery encoding (ADR-0030). Export is delivery-only and
same-orientation (α8.5a Fork F): ``format`` selects the container/codec, ``quality`` the
resolution ladder, ``orientation`` must match the master's orientation (a cross-orientation
request is a ``422``).

* :class:`ExportJobCreateRequest` — ``POST`` body: ``format`` / ``quality`` / ``orientation``
  (all validated against the ``export_*`` enums via ``Literal``). ``extra="forbid"`` rejects
  stray keys.
* :class:`ExportJobPublic` — the response projection of
  :class:`app.domain.export.export_job.ExportJob`, including the self-OCC ``version`` and the
  worker-owned lifecycle fields (``output_media_asset_id`` / ``file_size_bytes`` /
  ``finished_at``), which stay at their queued defaults until the export worker settles it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# Physical ``export_*`` ENUMs (``enums.py`` / baseline) — validated on the wire.
ExportFormatLiteral = Literal["mp4", "mov", "gif", "webm"]
ExportQualityLiteral = Literal["sd", "hd_1080p", "qhd_2k", "uhd_4k"]
ExportOrientationLiteral = Literal["horizontal", "vertical", "square"]


class ExportJobCreateRequest(BaseModel):
    """POST body: the requested delivery encoding for a completed render's master."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    format: ExportFormatLiteral
    quality: ExportQualityLiteral
    orientation: ExportOrientationLiteral


class ExportJobPublic(BaseModel):
    """Public projection of :class:`app.domain.export.export_job.ExportJob`."""

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
