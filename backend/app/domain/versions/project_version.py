"""``ProjectVersion`` domain entity — the Project Versions aggregate root.

Mirrors the ``project_versions`` table (``schema.md`` §9): an **immutable**
content snapshot of a project plus its ordered scenes, captured at a point in
time. The row is protected by a DB ``reject_mutation`` trigger (α5d DS7) — it
is never updated or (soft-)deleted, so the entity carries no ``updated_at`` /
``deleted_at`` / row-OCC ``version`` of its own. Carries **no** ORM
inheritance and no SQLAlchemy awareness; frozen for value-semantics, the same
discipline as :class:`app.domain.projects.project.Project`.

Two distinct "versions" deliberately coexist across the project aggregate
(PROJECT_AGGREGATE.md §6 / ADR-0035):

* ``projects.version`` — the optimistic-concurrency row counter
  (``VersionMixin``), bumped on every live-row mutation. NOT modelled here.
* ``project_versions.version_number`` — the **monotonic content-snapshot
  ordinal** (1, 2, 3 …) per project. THIS entity's ordering key. The two must
  never be conflated.

Key modelling decisions (α5d pre-flight / ADR-0035):

* ``snapshot`` — the denormalized JSONB blob (``schema_version`` + project +
  default storyboard + ordered scenes). Self-describing and content-versioned
  so future snapshot shapes migrate cleanly (Q7/Q10). Scene ``id`` values are
  preserved verbatim so a later restore can round-trip stable identities
  (α5c Q8).
* ``parent_version_id`` — the lineage pointer to the version that was current
  when this one was captured (``None`` for the first). Forms a linear chain
  for manual saves; branching is a later slice.
* ``reason`` — why the snapshot was taken (``manual_save`` in α5d.1; the other
  ``version_reason`` enum values — ``autosave`` / ``restore`` / ``branch`` /
  ``generated`` — are server concerns for later slices).
* ``diff_summary`` — optional precomputed delta vs the parent; always ``None``
  in α5d.1 (diffing is deferred).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProjectVersion:
    """Project Versions aggregate root — one immutable ``project_versions`` row."""

    id: UUID
    project_id: UUID
    version_number: int
    parent_version_id: UUID | None
    created_by_user_id: UUID
    reason: str
    snapshot: dict[str, Any]
    diff_summary: dict[str, Any] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectVersionSummary:
    """Metadata-only read model for the version-list endpoint (α5d Q4).

    Deliberately omits the (potentially large) ``snapshot`` / ``diff_summary``
    JSONB blobs: listing a project's version history returns only the
    lightweight metadata, and the full snapshot is fetched on demand by the
    single-version GET. The repository selects just these columns so a list of
    many versions never drags their snapshots off the DB.
    """

    id: UUID
    project_id: UUID
    version_number: int
    parent_version_id: UUID | None
    created_by_user_id: UUID
    reason: str
    created_at: datetime
