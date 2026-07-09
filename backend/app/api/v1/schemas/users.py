"""DTOs for ``/api/v1/users/*`` endpoints (Slice α3.3 covers ``GET /me``).

``UserPublic`` is the canonical public projection of the domain ``User``
entity. It lives here (not in ``schemas/auth.py``) so that user-shaped
DTOs and auth-shaped DTOs stop sharing a module — future ``/users/{id}``
and ``PATCH /users/me`` endpoints extend this file without bloating the
auth surface. See pre-flight §2.D6.

Field selection is deliberate: only attributes the API is contractually
allowed to return are declared here. Adding a new sensitive field to the
domain ``User`` entity does **not** leak into the response unless this
DTO is edited to include it — Pydantic v2 with explicit fields (rather
than ``model_config`` ``exclude``) gives compile-time safety.

Fields intentionally omitted:

* ``password_hash`` — secret, never leaves the server.
* ``last_login_at`` — internal audit signal, not part of the public
  identity projection (may become a separate endpoint later).
* ``updated_at`` / ``version`` — optimistic-concurrency plumbing, not
  business data.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class UserPublic(BaseModel):
    """Public projection of :class:`app.domain.identity.user.User`."""

    id: UUID
    tenant_id: UUID
    email: str
    display_name: str
    email_verified_at: datetime | None
    created_at: datetime
