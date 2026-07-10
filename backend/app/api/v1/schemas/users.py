"""DTOs for ``/api/v1/users/*`` endpoints.

α3.3 introduced :class:`UserPublic` for ``GET /users/me``.
α4 extends this module with:

* Two new fields on :class:`UserPublic` — ``version`` and ``updated_at``.
  These moved from "internal plumbing, intentionally omitted" (α3.3's
  stance) to "part of the public projection" because the α4
  ``PATCH /users/me`` endpoint requires the client to round-trip
  ``version`` as its optimistic-concurrency fence (pre-flight §D5 +
  Q1 resolution), and A12 asserts observable ``updated_at`` mutation
  through the response body.
* :class:`UpdateUserProfileRequest` — request DTO for
  ``PATCH /users/me``. Enforces the whitelist of patchable fields
  via ``extra="forbid"`` (pre-flight §D6 + §A6) and the caller-
  provided version fence.

Field selection remains deliberate: only attributes the API is
contractually allowed to return are declared here. Adding a new
sensitive field to the domain ``User`` entity does **not** leak into
the response unless this DTO is edited to include it — Pydantic v2
with explicit fields (rather than ``model_config`` ``exclude``) gives
compile-time safety.

Fields still intentionally omitted from :class:`UserPublic`:

* ``password_hash`` — secret, never leaves the server.
* ``last_login_at`` — internal audit signal, not part of the public
  identity projection (may become a separate endpoint later).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UserPublic(BaseModel):
    """Public projection of :class:`app.domain.identity.user.User`."""

    id: UUID
    tenant_id: UUID
    email: str
    display_name: str
    email_verified_at: datetime | None
    created_at: datetime
    # α4 additions — see module docstring for the rationale.
    #
    # ``version`` is the client's optimistic-concurrency handle: every
    # response returning a :class:`UserPublic` (register / login /
    # refresh / GET /me / PATCH /me) carries the *current* version;
    # the client sends it back in the next PATCH body as
    # :attr:`UpdateUserProfileRequest.version`.
    #
    # ``updated_at`` is exposed so clients can implement "last modified"
    # UX (cache invalidation, "changes saved just now" banners) without
    # a separate endpoint, and so integration tests can assert
    # observable mutation timing (A12).
    updated_at: datetime
    version: int


class UpdateUserProfileRequest(BaseModel):
    """PATCH /api/v1/users/me body.

    Whitelist of patchable fields is enforced by
    ``model_config = ConfigDict(extra="forbid")``: any body key not
    declared below (e.g. ``email``, ``password``, ``tenant_id``,
    ``created_at``) triggers a 422 ``VALIDATION_FAILED`` at the DTO
    boundary — the request never reaches the use case. This is the
    single place in the code where the "what can this endpoint mutate?"
    question is answered.

    Pre-flight anchors:

    * §D6  — HTTP method + status codes (this DTO owns the 422
      unknown-field rejection path).
    * §D7  — empty-body rejection: ``display_name`` is required.
      A body of ``{}`` or ``{"version": 5}`` is a 422 because a
      required field is missing.
    * §Q1  — ``version`` is required (no last-write-wins fallback).
    * §Q2  — ``display_name`` does not accept ``null`` (Pydantic
      rejects ``None`` for a non-optional field).
    * §A9  — length + shape validation lives here.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    display_name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "New display name. Whitespace is stripped before length "
            "validation; a whitespace-only value is rejected as empty "
            "(min_length=1 fails on the post-strip string)."
        ),
    )
    version: int = Field(
        ge=1,
        description=(
            "The ``version`` value the client last observed on the "
            "target user (from a prior GET/PATCH response). The server "
            "uses this as an optimistic-concurrency fence: a value "
            "that does not match the persisted row produces a "
            "412 VERSION_CONFLICT (never a silent overwrite)."
        ),
    )
