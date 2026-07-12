"""``Timeline`` domain entity — the Timeline bounded-context aggregate root.

Mirrors a **slim projection** of the ``timelines`` table (``schema.md`` §14 /
``models/timeline.py``). Carries **no** ORM inheritance and no SQLAlchemy
awareness. Frozen for value-semantics: mutations return new instances via
``dataclasses.replace`` at the repository/use-case layer — the same discipline
as :class:`app.domain.scenes.scene.Scene`.

Key modelling decisions (TIMELINE_AGGREGATE.md / α6.3 pre-flight):

* The timeline is the **aggregate root** and is **1:1 with a project** (the
  ``timelines`` table has a partial-unique index on ``project_id`` where
  ``deleted_at IS NULL``). Ownership is **derived through the project** — the
  table carries no ``tenant_id`` / ``owner_user_id``; the use case establishes
  ownership via ``IProjectRepository.get_owned`` before any timeline access.
* **It carries its own ``version``** (unlike prompts/media). ``timelines.version``
  is the **single OCC token for the whole aggregate** (root + tracks + clips):
  a fenced timeline/track/clip mutation compares against it and bumps it
  (ADR-0038 / Q1 / Q13). Children (``Track``, ``Clip``) have no version of their
  own — the timeline's is theirs.
* It is **excluded** from ``project_versions`` snapshots and does **not** bump
  ``projects.version`` (ADR-0035): a timeline edit is a composition change, not
  versioned editorial content.
* ``project_version_id`` — an optional provenance link to the project version the
  timeline was composed against. Its **write path is deferred to α7+** (ADR-0035);
  α6.3 leaves it ``None`` and surfaces it read-only.
* ``aspect_ratio`` — free text (e.g. ``'16:9'``), NOT NULL with no DB default; the
  provision use case sources it from the request or derives it from the project's
  ``aspect_ratio`` enum. ``frame_rate`` (1–240, CHECK), ``background_color`` (hex),
  ``duration_seconds`` — mutable root fields.
* ``id`` — a durable identity, minted once (``gen_random_uuid()``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Timeline:
    """Timeline aggregate root — one row of the ``timelines`` table (slim view)."""

    id: UUID
    project_id: UUID
    project_version_id: UUID | None
    duration_seconds: float
    aspect_ratio: str
    frame_rate: int
    background_color: str
    version: int
    created_at: datetime
    updated_at: datetime
