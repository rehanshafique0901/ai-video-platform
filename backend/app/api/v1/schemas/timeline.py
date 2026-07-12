"""DTOs for ``/api/v1/projects/{project_id}/timeline/*`` endpoints (α6.3a).

The Timeline aggregate is a **self-contained OCC aggregate** (ADR-0038): the
``timelines`` root carries a ``version`` that fences the whole tree (root +
tracks + clips). These DTOs mirror the ``schemas/scenes.py`` discipline
(``extra="forbid"``; tri-state PATCH via ``model_dump(exclude_unset=…)``; a
required ``version`` fence on root/child PATCH):

* :class:`TimelineProvisionRequest` — ``POST …/timeline`` body. All fields
  optional; ``aspect_ratio`` defaults from the project orientation server-side
  when omitted (Q3).
* :class:`TimelineUpdateRequest` — ``PATCH …/timeline`` body. ``version`` (the
  aggregate fence) is required; at least one mutable root field must accompany it.
* :class:`TrackCreateRequest` — ``POST …/timeline/tracks`` body. ``version`` is
  **optional** (a child create cannot be harmfully stale, Q13); ``z_index`` is
  client-assigned (a collision is a ``409``, Q5).
* :class:`TrackUpdateRequest` — ``PATCH …/timeline/tracks/{id}`` body. ``version``
  (the timeline's) is required; at least one mutable track field must accompany it.
* :class:`TimelinePublic` / :class:`TrackPublic` — response projections.
  ``TrackPublic`` has **no ``version``** (tracks share the timeline's token,
  surfaced in the response ``meta`` as ``timeline_version``).

``kind`` is validated against the ``track_kind`` enum; ``frame_rate`` against the
``frame_rate BETWEEN 1 AND 240`` CHECK; ``background_color`` / ``aspect_ratio``
against well-formed patterns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Physical ``track_kind`` enum (``enums.py`` / baseline 0001).
TrackKind = Literal["video", "audio", "subtitle", "effect"]

_MAX_NAME = 200
_ASPECT_RATIO_PATTERN = r"^\d{1,4}:\d{1,4}$"  # e.g. "16:9", "9:16", "1:1"
_HEX_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"  # e.g. "#000000"
_MIN_FPS = 1
_MAX_FPS = 240


class TimelineProvisionRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/timeline body (explicit creation, Q3).

    All fields optional: ``aspect_ratio`` is derived from the project orientation
    when omitted; ``frame_rate`` / ``background_color`` fall back to sensible
    defaults (matching the DB server-defaults).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    aspect_ratio: str | None = Field(default=None, pattern=_ASPECT_RATIO_PATTERN)
    frame_rate: int = Field(default=30, ge=_MIN_FPS, le=_MAX_FPS)
    background_color: str = Field(default="#000000", pattern=_HEX_COLOR_PATTERN)


class TimelineUpdateRequest(BaseModel):
    """PATCH /api/v1/projects/{project_id}/timeline body (version-fenced).

    ``version`` is the aggregate OCC token (required); ``aspect_ratio`` /
    ``frame_rate`` / ``background_color`` / ``duration_seconds`` are the mutable
    root fields. Tri-state via ``model_dump(exclude_unset=True, exclude={"version"})``.
    Empty patch (version only) → 422.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(ge=1)
    aspect_ratio: str | None = Field(default=None, pattern=_ASPECT_RATIO_PATTERN)
    frame_rate: int | None = Field(default=None, ge=_MIN_FPS, le=_MAX_FPS)
    background_color: str | None = Field(default=None, pattern=_HEX_COLOR_PATTERN)
    duration_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        """Reject a version-only patch (no mutable root field supplied)."""
        if not (self.model_fields_set - {"version"}):
            raise ValueError(
                "at least one mutable field (aspect_ratio, frame_rate, "
                "background_color, duration_seconds) is required"
            )
        return self


class TrackCreateRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/timeline/tracks body.

    ``version`` is **optional** (a child create cannot be harmfully stale — Q13);
    when present it fences the aggregate token, when absent the token is bumped
    unconditionally. ``z_index`` is client-assigned and unique per live timeline
    (a collision is a 409, Q5).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    kind: TrackKind
    z_index: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=_MAX_NAME)
    locked: bool = False
    muted: bool = False
    version: int | None = Field(default=None, ge=1)


class TrackUpdateRequest(BaseModel):
    """PATCH /api/v1/projects/{project_id}/timeline/tracks/{track_id} body.

    ``version`` (the **timeline's** token — tracks have no version of their own)
    is required; ``kind`` / ``z_index`` / ``name`` / ``locked`` / ``muted`` are the
    mutable track fields. Tri-state via
    ``model_dump(exclude_unset=True, exclude={"version"})``. Empty patch → 422.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(ge=1)
    kind: TrackKind | None = Field(default=None)
    z_index: int | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME)
    locked: bool | None = Field(default=None)
    muted: bool | None = Field(default=None)

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        """Reject a version-only patch (no mutable track field supplied)."""
        if not (self.model_fields_set - {"version"}):
            raise ValueError(
                "at least one mutable field (kind, z_index, name, locked, muted) is required"
            )
        return self


class TrackPublic(BaseModel):
    """Public projection of :class:`app.domain.timeline.track.Track`.

    **No ``version``** — a track shares the timeline's OCC token, surfaced in the
    response ``meta`` block as ``timeline_version`` (ADR-0038).
    """

    id: UUID
    timeline_id: UUID
    kind: str
    z_index: int
    name: str
    locked: bool
    muted: bool
    created_at: datetime
    updated_at: datetime


class TimelinePublic(BaseModel):
    """Public projection of :class:`app.domain.timeline.timeline.Timeline`.

    Carries the aggregate ``version`` (the OCC token) and the embedded ordered
    ``tracks`` (the composition tree; α6.3b adds each track's clips).
    ``project_version_id`` is surfaced read-only (its write path is deferred to
    α7+, ADR-0035).
    """

    id: UUID
    project_id: UUID
    project_version_id: UUID | None
    aspect_ratio: str
    frame_rate: int
    background_color: str
    duration_seconds: float
    version: int
    created_at: datetime
    updated_at: datetime
    tracks: list[TrackPublic]
