"""DTOs for ``/api/v1/projects/{project_id}/versions/*`` endpoints (α5d.1).

Mirrors the discipline in ``schemas/scenes.py``:

* :class:`ProjectVersionCreateRequest` — ``POST …/versions`` body. α5d.1
  captures take **no client input** (``reason`` is server-set to
  ``manual_save`` — Q5), so the model has no fields; ``extra="forbid"`` turns
  any supplied key into a 422. The endpoint accepts it as an optional body so
  a bare ``POST`` (no body) also works.
* :class:`ProjectVersionPublic` — metadata projection for the LIST endpoint
  (Q4): identity + lineage + provenance, but **no** snapshot blob.
* :class:`ProjectVersionDetail` — ``ProjectVersionPublic`` plus the full
  immutable ``snapshot`` (and ``diff_summary``, always ``null`` in α5d.1).
  Returned by create + single-version GET.

Versions are **addressed by UUID ``id``** in the path (α5d Q3), keeping the
whole API UUID-addressed like projects/scenes; ``version_number`` is the
user-facing label carried in the body, not the routing key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectVersionCreateRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/versions body (α5d.1: no fields).

    ``reason`` is server-set to ``manual_save`` (Q5); there is nothing for the
    client to supply yet. ``extra="forbid"`` rejects any provided key with a
    422. Later slices (labels / notes, non-manual reasons) extend this model.
    """

    model_config = ConfigDict(extra="forbid")


class ProjectVersionPublic(BaseModel):
    """Metadata projection of a project version (LIST view — no snapshot).

    ``is_current`` is derived (``id == projects.current_version_id``), not a
    stored column: "which version is current" is a property of the project,
    resolved by the use case from the ownership-gate ``Project`` and attached
    at the router. In α5d.1 (linear manual saves) the newest version is always
    current, but the flag is computed against the authoritative pointer so it
    stays correct once restore/branch (α5d.2) can make an *older* version
    current again.
    """

    id: UUID
    project_id: UUID
    version_number: int
    reason: str
    parent_version_id: UUID | None
    created_by_user_id: UUID
    created_at: datetime
    is_current: bool


class ProjectVersionDetail(ProjectVersionPublic):
    """Single-version projection: metadata + the immutable snapshot blob.

    ``snapshot`` is the denormalized, self-describing content record
    (``schema_version`` + project + default storyboard + ordered scenes).
    ``diff_summary`` is always ``null`` in α5d.1 (diffing is deferred).
    """

    snapshot: dict[str, Any]
    diff_summary: dict[str, Any] | None


class ProjectVersionRestoreRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/versions/{version_id}/restore body (α5d.2).

    ``version`` is the **aggregate** OCC token the client last observed on the
    project (§4 Aggregate OCC Rule) — restore is version-fenced exactly like a
    PATCH: a stale value yields ``412`` with no writes. ``extra="forbid"``
    rejects any other key with a 422. Required body (unlike the empty-body
    capture): the fence is mandatory.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(..., ge=1, description="Aggregate project version the client last saw.")


class ProjectVersionDiff(BaseModel):
    """Coarse base→target change summary between two versions (α5d.2 §7).

    Computed on demand; ``base`` is the ``against`` version, ``target`` is the
    path version (Q8). ``project_changed`` flags any project business-column
    difference; the scene counts are keyed on scene ``id``. Field-level detail
    is deferred (α5d.3+).
    """

    base_version_number: int
    target_version_number: int
    project_changed: bool
    scene_changes: SceneChangeCounts


class SceneChangeCounts(BaseModel):
    """Scene-set delta (base → target), keyed on scene ``id``."""

    added: int
    removed: int
    modified: int


ProjectVersionDiff.model_rebuild()
