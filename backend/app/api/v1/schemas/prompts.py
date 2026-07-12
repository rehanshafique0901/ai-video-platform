"""DTOs for ``/api/v1/projects/{project_id}/prompts/*`` endpoints (α6.1).

Mirrors the discipline in ``schemas/scenes.py``:

* :class:`PromptCreateRequest` — ``POST …/prompts`` body. ``extra="forbid"``
  turns any non-declared key (``id``, ``project_id``, ``generated_by_agent``,
  ``created_at``, …) into a 422 — identity and provenance are server-owned
  (α6.1 Q5).
* :class:`PromptPublic` — the response projection. **No** ``version`` field:
  prompts carry no optimistic-concurrency token (ADR-0036 / Q1 = Option A).
  ``generated_by_agent`` and ``deleted_at`` are omitted (server-internal, Q11).
* :class:`PromptUpdateRequest` — ``PATCH …/prompts/{id}`` body: partial,
  content-only, **no ``version``** (prompts are last-writer-wins). Tri-state
  resolved by the router via ``model_dump(exclude_unset=True)``. ``scene_id``
  is **not** accepted (immutable — no re-parenting in α6.1, Q10);
  ``extra="forbid"`` rejects it.

``kind`` is validated against the 8 ``prompt_kind`` enum values (F5); the DTO
never accepts chat-style ``system``/``user`` kinds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The physical ``prompt_kind`` enum (``enums.py`` / baseline 0001, F5). These
# are modality/aspect kinds — NOT chat-style system/user roles.
PromptKind = Literal[
    "image",
    "video",
    "animation",
    "negative",
    "camera",
    "motion",
    "lighting",
    "style",
]

_MAX_TEXT_CONTENT = 10_000


class PromptCreateRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/prompts body.

    ``kind`` + ``text_content`` are required; ``scene_id`` / ``model_id`` are
    optional links (validated in the use case — a foreign scene / unknown model
    → 422); ``extra`` is an optional free-form JSON object (default ``{}``).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    kind: PromptKind
    text_content: str = Field(min_length=1, max_length=_MAX_TEXT_CONTENT)
    scene_id: UUID | None = Field(default=None)
    model_id: UUID | None = Field(default=None)
    extra: dict[str, Any] = Field(default_factory=dict)


class PromptPublic(BaseModel):
    """Public projection of :class:`app.domain.prompts.prompt.Prompt`.

    ``project_id`` echoes the path. No ``version`` (prompts are not
    concurrency-controlled — ADR-0036); ``generated_by_agent`` / ``deleted_at``
    are server-internal and omitted (Q11).
    """

    id: UUID
    project_id: UUID
    scene_id: UUID | None
    kind: str
    text_content: str
    model_id: UUID | None
    extra: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PromptUpdateRequest(BaseModel):
    """PATCH /api/v1/projects/{project_id}/prompts/{prompt_id} body.

    Partial, content-only update. **No ``version``** — prompts have no OCC
    fence (ADR-0036 / Q1 = A), so PATCH is last-writer-wins. Tri-state (absent =
    unchanged; explicit ``null`` = clear a nullable column; value = set) is
    resolved by the router via ``model_dump(exclude_unset=True)``; the field
    defaults below are inert placeholders that exist only to make the fields
    optional. ``text_content`` / ``kind`` are non-nullable (an explicit ``null``
    is a 422); ``model_id`` is nullable so an explicit ``null`` clears the link.
    ``scene_id`` is NOT accepted (immutable — no re-parenting, Q10);
    ``extra="forbid"`` rejects it. Empty patch → 422.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text_content: str = Field(default="", min_length=1, max_length=_MAX_TEXT_CONTENT)
    kind: PromptKind = Field(default="image")
    model_id: UUID | None = Field(default=None)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        """Reject an empty patch (no mutable field supplied)."""
        if not self.model_fields_set:
            raise ValueError(
                "at least one mutable field (text_content, kind, model_id, extra) is required"
            )
        return self
