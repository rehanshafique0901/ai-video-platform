"""Result value objects for the Timeline use cases (Slice α6.3a).

Kept as tiny frozen dataclasses (mirroring ``scenes/results.py``) so the use
cases return richer-than-entity shapes without leaking DTOs into the application
layer:

* :class:`TimelineResult` — the timeline root plus its ordered live tracks (the
  composition tree the API projects for ``GET`` / ``POST /timeline``). α6.3b will
  extend the tree with each track's clips.
* :class:`TrackResult` — a single track plus the **current aggregate OCC token**
  (``timeline_version``) after the mutation, so the client can carry it into the
  next fenced timeline/track write (ADR-0038 / Q13).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.timeline.timeline import Timeline
from app.domain.timeline.track import Track


@dataclass(frozen=True, slots=True)
class TimelineResult:
    """A timeline root with its ordered live tracks."""

    timeline: Timeline
    tracks: list[Track]


@dataclass(frozen=True, slots=True)
class TrackResult:
    """A single track plus the aggregate OCC token (``timelines.version``) after the write."""

    track: Track
    timeline_version: int
