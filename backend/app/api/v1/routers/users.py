"""``/api/v1/users/*`` HTTP router.

Slice α3.3 introduced ``GET /me`` — the reference implementation of the
authenticated-read pattern. Slice α4 adds ``PATCH /me`` — the reference
implementation of the **canonical authenticated mutation pattern**:

1. Declare a single :data:`CurrentUserDep` parameter — no JWT parsing,
   no ``ITokenIssuer`` import, no repository access.
2. Accept a whitelisted request DTO
   (:class:`~app.api.v1.schemas.users.UpdateUserProfileRequest`) — Pydantic
   validation (``extra="forbid"``, ``ge=1`` on ``version``, ``min_length=1``
   on ``display_name``) enforces the 422 rejection surface.
3. Delegate business logic + optimistic concurrency to a use case
   (:class:`~app.application.use_cases.users.update_profile.UpdateUserProfile`).
   The use case runs the version-fenced CAS via
   :meth:`IUserRepository.update_profile`, emits structured logs, and
   raises :class:`~app.core.errors.VersionConflictError` on mismatch —
   the core exception handler renders that as a 412 with error code
   ``VERSION_CONFLICT``.
4. Project the returned domain :class:`~app.domain.identity.user.User`
   into :class:`~app.api.v1.schemas.users.UserPublic` (including the
   post-mutation ``version`` — the client uses it as the next-PATCH
   fence without a separate ``GET``).
5. Return the API_CONTRACT §1.1 envelope
   ``{ "data": ..., "meta": { "request_id": ... } }`` with status 200.

Both success (real change) and same-value no-op return 200 with the
current representation. In the no-op case, ``version`` is unchanged
(per pre-flight §D6a — version increments only when a persisted field
changes). This is a wire-level invariant future endpoints inherit.

Future authenticated mutation endpoints should copy this shape exactly.
See ``docs/engineering/AUTH_ENDPOINTS.md`` §8 for the canonical flow.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.deps import CurrentUserDep, UpdateUserProfileDep
from app.api.v1.helpers import client_ip, envelope
from app.api.v1.schemas.users import UpdateUserProfileRequest, UserPublic
from app.domain.identity.user import User

router = APIRouter(prefix="/users", tags=["users"])


def _to_public(user: User) -> UserPublic:
    """Project a domain ``User`` into the wire DTO ``UserPublic``.

    Single source of truth for the projection — used by both ``GET /me``
    and ``PATCH /me`` (α4). If a future endpoint needs the same shape,
    it should call this helper rather than re-inlining the constructor.
    """
    return UserPublic(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        email_verified_at=user.email_verified_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
        version=user.version,
    )


@router.get("/me")
async def get_me(request: Request, current_user: CurrentUserDep) -> JSONResponse:
    """Return the authenticated caller's own public profile.

    The entire authentication decision — token parsing, signature
    verification, session liveness, user existence — is performed inside
    :func:`~app.api.v1.deps.get_current_user`. By the time this handler
    runs, ``current_user`` is guaranteed to be a live, un-revoked user
    with a valid session; no additional validation is warranted.
    """
    return JSONResponse(content=envelope(_to_public(current_user), request))


@router.patch("/me")
async def update_me(
    body: UpdateUserProfileRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: UpdateUserProfileDep,
) -> JSONResponse:
    """Update the authenticated caller's profile.

    Currently the only field a caller can update through this endpoint is
    ``display_name`` (see pre-flight §1.3 non-goals — no avatar, no
    timezone, no locale, no bio, no phone). The request body also
    requires the client's last-observed ``version`` so the use case can
    execute a compare-and-swap update at the repository layer; that fence
    is the optimistic-concurrency mechanism guarding against concurrent
    updates from parallel sessions.

    Return semantics (all 200):

    * Persisted-field change → ``version`` in the response is the new
      (incremented) version; ``updated_at`` reflects the DB write.
    * Same-value no-op (``display_name`` equals the current value) →
      ``version`` and ``updated_at`` unchanged; no DB write. The client
      still receives 200 with the current representation, so it can
      confirm its optimistic view is authoritative.

    Failure modes:

    * ``version`` in the body does not match the DB row's ``version`` →
      use case raises ``VersionConflictError`` → 412 with error code
      ``VERSION_CONFLICT``. Also returned for a soft-deleted user (an
      anti-enumeration decision; the client cannot distinguish it from a
      race).
    * Validation (empty name, non-string, ``version < 1``, extra fields,
      ``null`` for ``display_name``) → 422 via Pydantic before this
      handler is entered.
    * Missing / invalid auth → 401 via ``get_current_user``.
    """
    result = await use_case.execute(
        user_id=current_user.id,
        expected_version=body.version,
        display_name=body.display_name,
        ip=client_ip(request),
    )
    return JSONResponse(content=envelope(_to_public(result.user), request))
