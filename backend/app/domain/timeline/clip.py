"""``Clip`` domain entity — a child of the Timeline aggregate (Slice α6.3b).

Mirrors a **slim projection** of the ``clips`` table (``schema.md`` §14 /
``models/timeline.py``). Frozen for value-semantics, no SQLAlchemy awareness —
the same discipline as :class:`app.domain.timeline.track.Track`.

Key modelling decisions (PHASE3_ALPHA6_3B_PREFLIGHT.md / α6.3 sign-off):

* A clip is a **child of the timeline aggregate** (via its track), not an
  aggregate root: it has **no ``version`` column** (the ``clips`` table carries
  no ``VersionMixin`` and is absent from the version-bump trigger set). The
  aggregate OCC token is the parent ``timelines.version`` (ADR-0038 / Q13) — a
  clip create/update/delete is fenced against, and bumps, the timeline's version.
* ``track_id`` — the parent link (``ON DELETE CASCADE``; tracks are only
  soft-deleted via the API, so cascade never fires). Immutable in α6.3b — moving
  a clip between tracks is delete + recreate (Q4).
* ``media_asset_id`` — an **optional** link to a registered media asset
  (``ON DELETE SET NULL``). When set, the use case validates it is an owned, live
  asset (→ ``422``, D4); the FK alone would silently accept a foreign/dead asset.
* ``start_seconds`` / ``end_seconds`` — the clip's placement on the timeline
  (CHECK ``start >= 0``, ``end > start``). ``source_start_seconds`` /
  ``source_end_seconds`` — the trim window into the source media (default 0,
  ``source_end >= source_start``, Q2). Clips may overlap and need not fit the
  timeline duration (Q6 / D5).
* ``volume`` (0–4, CHECK), ``locked`` — mutable clip attributes.
* ``transition_in_id`` / ``transition_out_id`` / ``effects`` — surfaced
  **read-only** in α6.3b; their write paths are deferred to α6.4 (D9 / Q1).
* ``id`` — a durable identity, minted once (``gen_random_uuid()``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Clip:
    """A clip under a track — one row of the ``clips`` table (slim view)."""

    id: UUID
    track_id: UUID
    media_asset_id: UUID | None
    start_seconds: float
    end_seconds: float
    source_start_seconds: float
    source_end_seconds: float
    volume: float
    locked: bool
    # Read-only in α6.3b (write paths deferred to α6.4 — D9).
    transition_in_id: UUID | None
    transition_out_id: UUID | None
    effects: list[Any]
    created_at: datetime
    updated_at: datetime
