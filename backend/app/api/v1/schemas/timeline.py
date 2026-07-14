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
* :class:`ClipCreateRequest` — ``POST …/tracks/{id}/clips`` body (α6.3b).
  ``version`` optional (child create, Q13); ``start``/``end`` required, ``source_*``
  optional (Q2); ``media_asset_id`` validated server-side (owned + live → 422, D4).
* :class:`ClipUpdateRequest` — ``PATCH …/clips/{id}`` body. ``version`` required;
  ``track_id`` immutable (no cross-track move, Q4).
* :class:`TimelinePublic` / :class:`TrackPublic` / :class:`ClipPublic` — response
  projections. ``TrackPublic`` / ``ClipPublic`` have **no ``version``** (children
  share the timeline's token, surfaced in the response ``meta`` as
  ``timeline_version``). ``TrackPublic.clips`` embeds each track's ordered clips
  in the composition-tree reads (D8).

``kind`` is validated against the ``track_kind`` enum; ``frame_rate`` against the
``frame_rate BETWEEN 1 AND 240`` CHECK; ``background_color`` / ``aspect_ratio``
against well-formed patterns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
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


class ClipCreateRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips body (α6.3b).

    ``version`` is **optional** (a child create cannot be harmfully stale — Q13);
    when present it fences the aggregate token, when absent the token is bumped
    unconditionally. Time model (D5 / Q2): ``start_seconds`` / ``end_seconds``
    required (``end > start``); ``source_start_seconds`` / ``source_end_seconds``
    optional (default 0, ``source_end >= source_start``) — the trim window into
    the source media. Clips may overlap (Q6). ``media_asset_id`` is validated
    server-side (owned + live → 422, D4). ``effects`` / ``transition_*`` have no
    write path in α6.3b (deferred to α6.4 — D9 / Q1).
    """

    model_config = ConfigDict(extra="forbid")

    media_asset_id: UUID | None = None
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    source_start_seconds: float = Field(default=0.0, ge=0)
    source_end_seconds: float = Field(default=0.0, ge=0)
    volume: float = Field(default=1.0, ge=0, le=4)
    locked: bool = False
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        """Enforce the DB CHECKs at the edge (→ 422, not a 500 from the CHECK)."""
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        if self.source_end_seconds < self.source_start_seconds:
            raise ValueError(
                "source_end_seconds must be greater than or equal to source_start_seconds"
            )
        return self


class ClipUpdateRequest(BaseModel):
    """PATCH /api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id} body.

    ``version`` (the **timeline's** token — clips have no version of their own) is
    required; the mutable clip fields are optional. ``track_id`` is immutable (no
    cross-track move, Q4). Tri-state via ``model_dump(exclude_unset=True,
    exclude={"version"})`` — so an explicit ``media_asset_id: null`` unlinks while
    an omitted one is unchanged. Empty patch (version only) → 422. Cross-field
    time checks fire only when *both* operands are present in this patch; the use
    case re-checks the *merged* range against the stored clip (→ 422) so a partial
    patch cannot slip past the DB CHECK into a 500.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    media_asset_id: UUID | None = Field(default=None)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    source_start_seconds: float | None = Field(default=None, ge=0)
    source_end_seconds: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0, le=4)
    locked: bool | None = Field(default=None)

    @model_validator(mode="after")
    def _require_and_check(self) -> Self:
        """Reject a version-only patch; validate any fully-supplied range pair."""
        if not (self.model_fields_set - {"version"}):
            raise ValueError(
                "at least one mutable field (media_asset_id, start_seconds, "
                "end_seconds, source_start_seconds, source_end_seconds, volume, "
                "locked) is required"
            )
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds <= self.start_seconds
        ):
            raise ValueError("end_seconds must be greater than start_seconds")
        if (
            self.source_start_seconds is not None
            and self.source_end_seconds is not None
            and self.source_end_seconds < self.source_start_seconds
        ):
            raise ValueError(
                "source_end_seconds must be greater than or equal to source_start_seconds"
            )
        return self


class ClipPublic(BaseModel):
    """Public projection of :class:`app.domain.timeline.clip.Clip`.

    **No ``version``** — a clip shares the timeline's OCC token, surfaced in the
    response ``meta`` block as ``timeline_version`` (ADR-0038). ``transition_*`` /
    ``effects`` are surfaced read-only (their write paths are deferred to α6.4 —
    D9).
    """

    id: UUID
    track_id: UUID
    media_asset_id: UUID | None
    start_seconds: float
    end_seconds: float
    source_start_seconds: float
    source_end_seconds: float
    volume: float
    locked: bool
    transition_in_id: UUID | None
    transition_out_id: UUID | None
    effects: list[Any]
    created_at: datetime
    updated_at: datetime


class TrackPublic(BaseModel):
    """Public projection of :class:`app.domain.timeline.track.Track`.

    **No ``version``** — a track shares the timeline's OCC token, surfaced in the
    response ``meta`` block as ``timeline_version`` (ADR-0038). ``clips`` is the
    track's ordered live clips — populated in the **composition-tree reads**
    (``GET …/timeline`` / ``GET …/tracks``, D8); track-mutation responses
    (``POST`` / ``PATCH …/tracks``) return it empty (the canonical clip reads are
    ``GET …/timeline`` / ``…/clips``).
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
    clips: list[ClipPublic] = Field(default_factory=list)


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
