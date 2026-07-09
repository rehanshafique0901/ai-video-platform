"""``/api/v1/users/*`` HTTP router (Slice α3.3 introduces ``GET /me``).

This router is the reference implementation of the α3 authenticated-
request pattern (see pre-flight §10 exit criteria). Every future
authenticated endpoint follows the same shape:

1. Declare a single :data:`CurrentUserDep` parameter — no JWT parsing,
   no ``ITokenIssuer`` import, no repository access.
2. Project the injected domain :class:`~app.domain.identity.user.User`
   into a wire DTO (:class:`~app.api.v1.schemas.users.UserPublic`).
3. Return the API_CONTRACT §1.1 envelope
   ``{ "data": ..., "meta": { "request_id": ... } }``.

If a future ``GET /users/me`` change requires talking to the database
(e.g. hydrating profile-preferences), that lookup belongs in a use case
called from this router — **not** in the router itself and **not** in
:func:`~app.api.v1.deps.get_current_user` (which owns only the
authentication seam).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.deps import CurrentUserDep
from app.api.v1.schemas.users import UserPublic

router = APIRouter(prefix="/users", tags=["users"])


def _envelope(payload: UserPublic, request: Request) -> dict[str, Any]:
    request_id = getattr(request.state, "request_id", "")
    return {"data": payload.model_dump(mode="json"), "meta": {"request_id": request_id}}


@router.get("/me")
async def get_me(request: Request, current_user: CurrentUserDep) -> JSONResponse:
    """Return the authenticated caller's own public profile.

    The entire authentication decision — token parsing, signature
    verification, session liveness, user existence — is performed inside
    :func:`~app.api.v1.deps.get_current_user`. By the time this handler
    runs, ``current_user`` is guaranteed to be a live, un-revoked user
    with a valid session; no additional validation is warranted.
    """
    payload = UserPublic(
        id=current_user.id,
        tenant_id=current_user.tenant_id,
        email=current_user.email,
        display_name=current_user.display_name,
        email_verified_at=current_user.email_verified_at,
        created_at=current_user.created_at,
        # α4 additions to UserPublic — see schemas/users.py module
        # docstring. `version` gives the client its next-PATCH fence
        # without a separate round-trip; `updated_at` lets the client
        # implement "last modified" UX.
        updated_at=current_user.updated_at,
        version=current_user.version,
    )
    return JSONResponse(content=_envelope(payload, request))
