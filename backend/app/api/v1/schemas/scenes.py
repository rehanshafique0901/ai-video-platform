"""DTOs for ``/api/v1/projects/{project_id}/scenes/*`` endpoints (α5c).

Mirrors the discipline in ``schemas/projects.py``:

* :class:`SceneCreateRequest` — ``POST …/scenes`` body. ``extra="forbid"``
  turns any non-declared key (``storyboard_id``, ``scene_number``,
  ``position``, ``version``, ``id``) into a 422 — ordering and identity are
  server-owned, never client-supplied (α5c D10/Q6).
* :class:`ScenePublic` — the response projection. Exposes a dense 1-based
  ``position`` (computed server-side) and **omits** ``storyboard_id`` and
  the raw sparse ``scene_number`` (α5c Q6): the storyboard is an
  implementation detail and clients must not depend on the internal
  ordering key.
* :class:`SceneUpdateRequest` — ``PATCH …/scenes/{id}`` body: partial,
  version-fenced, content-only (no ``position`` — reordering is
  :class:`SceneMoveRequest`, α5c Q1/D11). Tri-state resolved by the router
  via ``model_fields_set``.
* :class:`SceneMoveRequest` — ``POST …/scenes/{id}/move`` body: the
  version-fenced reorder (α5c Q1/Q4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Duration is persisted as ``Numeric(8,3)`` with a ``duration_seconds > 0``
# CHECK; the DTO enforces the positivity (so an invalid value is a 422 at the
# boundary, not a 500 from the DB constraint) and a sane 24h ceiling.
_MAX_DURATION_SECONDS = 86_400.0


class SceneCreateRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/scenes body.

    ``title`` / ``duration_seconds`` are required; ``narration`` /
    ``subtitle`` are optional free text. The scene is always appended at the
    end (α5c D10) — there is no ``position`` on create; use ``…/move``.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    duration_seconds: float = Field(gt=0, le=_MAX_DURATION_SECONDS)
    narration: str | None = Field(default=None, max_length=5000)
    subtitle: str | None = Field(default=None, max_length=2000)


class ScenePublic(BaseModel):
    """Public projection of :class:`app.domain.scenes.scene.Scene`.

    ``position`` is the dense 1-based display index derived from the sparse
    ``scene_number`` (never exposed). ``version`` is the client's OCC handle
    for the ``PATCH`` / ``move`` fences. ``project_id`` echoes the path
    (the domain entity carries only ``storyboard_id``, which is omitted).
    """

    id: UUID
    project_id: UUID
    position: int
    title: str
    duration_seconds: float
    narration: str | None
    subtitle: str | None
    created_at: datetime
    updated_at: datetime
    version: int


class SceneUpdateRequest(BaseModel):
    """PATCH /api/v1/projects/{project_id}/scenes/{scene_id} body.

    Partial, version-fenced, **content-only** update. Tri-state (absent =
    unchanged; explicit ``null`` = clear a nullable column; value = set) is
    resolved by the router via ``model_dump(exclude_unset=True)``; the field
    defaults below are inert placeholders that exist only to make the fields
    optional. ``title`` / ``duration_seconds`` are non-nullable (typed
    without ``| None``) so an explicit ``null`` is a 422; ``narration`` /
    ``subtitle`` are nullable so an explicit ``null`` clears them.
    ``position`` is NOT accepted here — reordering is :class:`SceneMoveRequest`
    (α5c Q1/D11); ``extra="forbid"`` rejects it.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(
        ge=1,
        description=(
            "The ``version`` the client last observed on the target scene. The "
            "server performs a compare-and-swap; a stale value yields 412."
        ),
    )
    title: str = Field(default="", min_length=1, max_length=200)
    duration_seconds: float = Field(default=0.0, gt=0, le=_MAX_DURATION_SECONDS)
    narration: str | None = Field(default=None, max_length=5000)
    subtitle: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        """Reject an empty patch (only ``version``, no content field)."""
        if not (set(self.model_fields_set) - {"version"}):
            raise ValueError(
                "at least one mutable field "
                "(title, duration_seconds, narration, subtitle) is required"
            )
        return self


class SceneMoveRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/scenes/{scene_id}/move body.

    A dedicated, version-fenced reorder (α5c Q1/Q4). ``position`` is 1-based;
    the server clamps out-of-range values into ``[1, N]`` and treats a move
    to the current slot as a no-op.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    position: int = Field(ge=1)
