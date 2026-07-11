"""``Scene`` domain entity — the Scenes bounded-context aggregate root.

Mirrors a **slim projection** of the ``scenes`` table (``schema.md`` §11):
the physical table is deliberately "fat" (carrying deferred cinematography
columns — ``emotion`` / ``camera_angle`` / ``lighting`` / … — see
``docs/domain/SCENE_AGGREGATE.md`` §4), but the α5c domain model exposes
only the fields the create/read/update surface actually uses. Carries
**no** ORM inheritance and no SQLAlchemy awareness. Frozen for
value-semantics: mutations return new instances via ``dataclasses.replace``
at the repository/use-case layer, keeping the entity immutable for safe
sharing across concurrent tasks — the same discipline as
:class:`app.domain.projects.project.Project`.

Key modelling decisions (SCENE_AGGREGATE.md / α5c pre-flight):

* ``storyboard_id`` — the physical parent. A project owns one *implicit*
  default storyboard (D1); this id is resolved server-side and is **not**
  exposed on the wire (``ScenePublic`` omits it — Q6).
* ``scene_number`` — the **internal** sparse gap-based ordering key (1000,
  2000, 3000 … — D3). Never exposed raw; the API projects a dense 1-based
  ``position`` computed from the sorted order (Q6).
* ``id`` — a durable identity, minted once and **stable across future
  Version restores** (pre-flight Q8/D16); never re-minted.
* ``version`` — the optimistic-concurrency counter (``VersionMixin``),
  bumped on every persisted content/order mutation; the α5c ``PATCH`` /
  ``move`` fences read it. The guarded ``bump_version()`` trigger means the
  repository hand-sets ``version + 1`` and the net increment is exactly +1
  (see ``ProjectRepository.update_owned``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Scene:
    """Scenes aggregate root — one row of the ``scenes`` table (slim view)."""

    id: UUID
    storyboard_id: UUID
    scene_number: int
    title: str
    duration_seconds: float
    narration: str | None
    subtitle: str | None
    created_at: datetime
    updated_at: datetime
    version: int
