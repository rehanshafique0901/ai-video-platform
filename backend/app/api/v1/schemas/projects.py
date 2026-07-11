"""DTOs for ``/api/v1/projects/*`` endpoints (α5a create/read; α5b update).

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
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ProjectUpdateRequest(BaseModel):
    """PATCH /api/v1/projects/{id} body — partial, version-fenced update.

    Pre-flight anchors (α5b §D4):

    * **Tri-state semantics.** Every mutable field is optional; the
      distinction between *absent* (leave unchanged), *explicit ``null``*
      (clear a nullable column), and *value* (set) is resolved by the
      router via ``model_fields_set`` (``model_dump(exclude_unset=True)``),
      NOT by the field defaults below. The defaults (``""`` /​ ``None`` /​
      ``{}``) are inert placeholders that only exist so the fields are
      optional; they are never read because an unset field is excluded
      from the ``changes`` mapping handed to the use case.
    * **``version`` required (§Q1).** The client's last-observed
      optimistic-concurrency handle. No last-write-wins fallback.
    * **Non-nullable fields reject ``null`` (§A4).** ``name`` / ``language``
      are typed ``str`` (not ``str | None``), so ``{"name": null}`` is a
      422 — clearing a required column is nonsensical. ``description`` /
      ``style`` are ``str | None`` so an explicit ``null`` *clears* them.
    * **Empty patch → 422 (§A10).** A body with only ``version`` and no
      mutable field is almost certainly a client bug; the
      ``_require_mutable_field`` validator rejects it rather than treating
      it as a 200 no-op.
    * **``aspect_ratio`` immutable (§Q3), ``folder_id`` create-time-only,
      server/identity fields forbidden (§A9).** Not declared here;
      ``extra="forbid"`` turns any such key into a 422.
    * **``settings`` whole-object replace (§Q5/§D5).** A present ``settings``
      replaces the entire JSONB object (no deep-merge).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(
        ge=1,
        description=(
            "The ``version`` the client last observed on the target project "
            "(from a prior POST/GET/PATCH response). The server performs a "
            "compare-and-swap; a stale value yields 412 VERSION_CONFLICT."
        ),
    )
    # Non-nullable optionals: ``str`` (not ``str | None``) so an explicit
    # ``null`` is a 422. The ``default=""`` is inert (never used — the router
    # keys off ``model_fields_set``); ``min_length=1`` still rejects an
    # explicitly-sent empty/whitespace-only value.
    name: str = Field(default="", min_length=1, max_length=200)
    language: str = Field(default="", min_length=1, max_length=8)
    # Nullable optionals: explicit ``null`` clears the column.
    description: str | None = Field(default=None, max_length=2000)
    style: str | None = Field(default=None, max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        """Reject an empty patch (only ``version``, no mutable field) — §A10."""
        if not (set(self.model_fields_set) - {"version"}):
            raise ValueError(
                "at least one mutable field "
                "(name, description, language, style, settings) is required"
            )
        return self
