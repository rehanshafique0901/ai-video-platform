"""``Track`` domain entity — a child of the Timeline aggregate.

Mirrors a **slim projection** of the ``tracks`` table (``schema.md`` §14 /
``models/timeline.py``). Frozen for value-semantics, no SQLAlchemy awareness.

Key modelling decisions (TIMELINE_AGGREGATE.md / α6.3 pre-flight):

* A track is a **child of the timeline**, not an aggregate root: it has **no
  ``version`` column** (the ``tracks`` table carries no ``VersionMixin`` and is
  absent from the version-bump trigger set). The aggregate OCC token is the
  parent ``timelines.version`` (ADR-0038 / Q13) — a track create/update/delete is
  fenced against, and bumps, the timeline's version.
* ``timeline_id`` — the parent link (``ON DELETE CASCADE``; timelines are only
  soft-deleted via the API, so cascade never fires). Scoping is by timeline; the
  use case has already established project → timeline ownership upstream.
* ``kind`` — one of the ``track_kind`` enum values (video / audio / subtitle /
  effect). Mutable.
* ``z_index`` — the stacking order, a **sparse integer unique per live timeline**
  (partial-unique index ``uq_tracks_timeline_id_z_index``). Client-assigned; a
  collision surfaces as ``409`` (Q5). Gaps are allowed (it is a stacking key, not
  a dense sequence).
* ``locked`` / ``muted`` / ``name`` — mutable track attributes.
* ``id`` — a durable identity, minted once (``gen_random_uuid()``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Track:
    """A track under a timeline — one row of the ``tracks`` table (slim view)."""

    id: UUID
    timeline_id: UUID
    kind: str
    z_index: int
    locked: bool
    muted: bool
    name: str
    created_at: datetime
    updated_at: datetime
