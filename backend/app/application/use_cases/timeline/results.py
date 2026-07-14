"""Result value objects for the Timeline use cases (Slices α6.3a / α6.3b).

Kept as tiny frozen dataclasses (mirroring ``scenes/results.py``) so the use
cases return richer-than-entity shapes without leaking DTOs into the application
layer:

* :class:`TimelineResult` — the timeline root plus its ordered live tracks and,
  since α6.3b, each track's ordered live clips (``clips_by_track``). This is the
  composition tree the API projects for ``GET`` / ``POST /timeline`` and
  ``GET …/tracks``.
* :class:`TrackResult` — a single track plus the **current aggregate OCC token**
  (``timeline_version``) after the mutation, so the client can carry it into the
  next fenced timeline/track/clip write (ADR-0038 / Q13).
* :class:`ClipResult` — a single clip plus the aggregate OCC token after the
  mutation (mirrors :class:`TrackResult`; clips have no version of their own).
* :class:`ClipListResult` — a track's ordered live clips plus the aggregate OCC
  token (for the flat ``GET …/clips``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.domain.timeline.clip import Clip
from app.domain.timeline.timeline import Timeline
from app.domain.timeline.track import Track


@dataclass(frozen=True, slots=True)
class TimelineResult:
    """A timeline root with its ordered live tracks and each track's clips.

    ``clips_by_track`` maps ``track.id`` → the track's live clips (``start_seconds``
    ASC); a track absent from the mapping has no live clips (the projection
    defaults to ``[]``). Empty by default so α6.3a-era callers that don't embed
    clips stay valid.
    """

    timeline: Timeline
    tracks: list[Track]
    clips_by_track: dict[UUID, list[Clip]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TrackResult:
    """A single track plus the aggregate OCC token (``timelines.version``) after the write."""

    track: Track
    timeline_version: int


@dataclass(frozen=True, slots=True)
class ClipResult:
    """A single clip plus the aggregate OCC token (``timelines.version``) after the write."""

    clip: Clip
    timeline_version: int


@dataclass(frozen=True, slots=True)
class ClipListResult:
    """A track's ordered live clips plus the aggregate OCC token (``timelines.version``)."""

    clips: list[Clip]
    timeline_version: int
