"""DTOs for ``/api/v1/projects/*`` endpoints (Slice α5a — create + read).

* :class:`ProjectCreateRequest` — ``POST /projects`` body. The
  whitelist of settable fields is enforced by
  ``model_config = ConfigDict(extra="forbid")``: any key not declared
  below (e.g. ``owner_user_id``, ``tenant_id``, ``version``, ``id``)
  is a 422 ``VALIDATION_FAILED`` at the DTO boundary — ownership and
  tenancy come from the authenticated caller, never the body (α5a D5).
* :class:`ProjectPublic` — the response projection. Deliberately omits
  ``current_version_id`` and ``duration_seconds`` (α5a D8): both are
  managed by later slices (version ledger / render output) and are
  always unset in α5a, so exposing them now would advertise a contract
  the endpoint does not yet honour.

Field selection is deliberate (same discipline as ``schemas/users.py``):
only attributes the API is contractually allowed to return are declared
here, so adding a field to the domain ``Project`` entity never leaks
into the wire response unless this DTO is edited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreateRequest(BaseModel):
    """POST /api/v1/projects body.

    Pre-flight anchors:

    * §D5  — ``owner_user_id`` / ``tenant_id`` are NOT accepted here;
      ``extra="forbid"`` rejects any attempt to set them.
    * §D7  — ``aspect_ratio`` is required (no hidden default) and
      constrained to the three values the ``projects.aspect_ratio``
      CHECK constraint permits, so an invalid value is a 422 at the DTO
      boundary rather than a 500 from a DB constraint violation.
    * §A4  — ``name`` is required, whitespace-stripped, 1..200 chars;
      a whitespace-only name fails ``min_length=1`` on the post-strip
      string.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    aspect_ratio: Literal["horizontal", "vertical", "square"]
    description: str | None = Field(default=None, max_length=2000)
    language: str = Field(default="en", min_length=1, max_length=8)
    style: str | None = Field(default=None, max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectPublic(BaseModel):
    """Public projection of :class:`app.domain.projects.project.Project`.

    ``version`` is the client's optimistic-concurrency handle for the
    α5b ``PATCH`` fence — every response carrying a ``ProjectPublic``
    reports the current version. ``updated_at`` supports "last modified"
    UX. ``current_version_id`` / ``duration_seconds`` are intentionally
    omitted (α5a D8).
    """

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    folder_id: UUID | None
    name: str
    description: str | None
    aspect_ratio: str
    language: str
    style: str | None
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    version: int
