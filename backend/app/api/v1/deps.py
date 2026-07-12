"""FastAPI dependency providers.

Imports are restricted to ``app.application.interfaces`` (ports) and
``app.core.container`` (composition root). Direct imports from
``app.infrastructure`` would violate the import-linter contract — the
container exists to hold the binding between the two so the API layer
never sees the implementations.

Slice α2a adds use-case dependency aliases so router handlers can
declare e.g. ``register_use_case: RegisterUserDep`` and receive a
fully-wired instance from the container.

Slice α3 adds the **authenticated-request seam**:

* ``get_current_user`` — resolves ``Authorization: Bearer <access>``
  into a live :class:`~app.domain.identity.user.User`. Strict
  verification (``allow_expired=False``); fail-closed on any
  non-happy branch; anti-enumeration generic 401 client-side with
  the specific reason on the server-side structured log.
* ``CurrentUserDep`` — the alias every authenticated endpoint uses.
  Per pre-flight §10, this is the *only* authentication seam future
  endpoints should reach for; direct JWT parsing or ``ITokenIssuer``
  access from a router is a review-blocker signal.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import client_ip
from app.application.interfaces.repositories import IUserRepository
from app.application.interfaces.security import ITokenIssuer
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.login_user import LoginUser
from app.application.use_cases.auth.logout_session import LogoutSession
from app.application.use_cases.auth.refresh_session import RefreshSession
from app.application.use_cases.auth.register_user import RegisterUser
from app.application.use_cases.media.delete_media import DeleteMedia
from app.application.use_cases.media.get_media import GetMedia
from app.application.use_cases.media.list_media import ListMedia
from app.application.use_cases.media.register_media import RegisterMedia
from app.application.use_cases.media.update_media import UpdateMedia
from app.application.use_cases.projects.create_project import CreateProject
from app.application.use_cases.projects.delete_project import DeleteProject
from app.application.use_cases.projects.get_project import GetProject
from app.application.use_cases.projects.list_projects import ListProjects
from app.application.use_cases.projects.update_project import UpdateProject
from app.application.use_cases.prompts.create_prompt import CreatePrompt
from app.application.use_cases.prompts.delete_prompt import DeletePrompt
from app.application.use_cases.prompts.get_prompt import GetPrompt
from app.application.use_cases.prompts.list_prompts import ListPrompts
from app.application.use_cases.prompts.update_prompt import UpdatePrompt
from app.application.use_cases.scenes.create_scene import CreateScene
from app.application.use_cases.scenes.delete_scene import DeleteScene
from app.application.use_cases.scenes.get_scene import GetScene
from app.application.use_cases.scenes.list_scenes import ListScenes
from app.application.use_cases.scenes.move_scene import MoveScene
from app.application.use_cases.scenes.update_scene import UpdateScene
from app.application.use_cases.users.update_profile import UpdateUserProfile
from app.application.use_cases.versions.branch_version import BranchProjectVersion
from app.application.use_cases.versions.create_version import CreateProjectVersion
from app.application.use_cases.versions.diff_versions import DiffProjectVersions
from app.application.use_cases.versions.get_version import GetProjectVersion
from app.application.use_cases.versions.list_versions import ListProjectVersions
from app.application.use_cases.versions.restore_version import RestoreProjectVersion
from app.core import container
from app.core.errors import UnauthorizedError
from app.domain.identity.user import User

_LOGGER = structlog.get_logger(__name__)

# ``_GENERIC_401`` — anti-enumeration: every failure branch in
# ``get_current_user`` raises with this exact message so the client
# cannot tell a signature-failure from a session-revoked from a
# soft-deleted user. Server-side ``auth.request.rejected`` structured
# log carries the specific reason for SIEM alerting (per pre-flight
# §2.D4 — same discipline as α2a login + α2b refresh).
_GENERIC_401 = "not authenticated"

SessionDep = Annotated[AsyncSession, Depends(container.get_session)]
UoWDep = Annotated[IUnitOfWork, Depends(container.get_unit_of_work)]
TokenIssuerDep = Annotated[ITokenIssuer, Depends(container.get_token_issuer)]


def get_user_repository(session: SessionDep) -> IUserRepository:
    """FastAPI sub-dependency: build a ``UserRepository`` from the request session."""
    return container.get_user_repository(session)


UserRepoDep = Annotated[IUserRepository, Depends(get_user_repository)]

# ---- Use-case dependencies (Slice α2a) --------------------------------

RegisterUserDep = Annotated[RegisterUser, Depends(container.get_register_user_use_case)]
LoginUserDep = Annotated[LoginUser, Depends(container.get_login_user_use_case)]

# ---- Use-case dependencies (Slice α2b) --------------------------------

RefreshSessionDep = Annotated[RefreshSession, Depends(container.get_refresh_session_use_case)]
LogoutSessionDep = Annotated[LogoutSession, Depends(container.get_logout_session_use_case)]

# ---- Use-case dependencies (Slice α4) ---------------------------------

UpdateUserProfileDep = Annotated[
    UpdateUserProfile, Depends(container.get_update_user_profile_use_case)
]

# ---- Use-case dependencies (Slice α5a) --------------------------------

CreateProjectDep = Annotated[CreateProject, Depends(container.get_create_project_use_case)]
GetProjectDep = Annotated[GetProject, Depends(container.get_get_project_use_case)]
ListProjectsDep = Annotated[ListProjects, Depends(container.get_list_projects_use_case)]
UpdateProjectDep = Annotated[UpdateProject, Depends(container.get_update_project_use_case)]
DeleteProjectDep = Annotated[DeleteProject, Depends(container.get_delete_project_use_case)]

# ---- Use-case dependencies (Slice α5c — Scenes) -----------------------

CreateSceneDep = Annotated[CreateScene, Depends(container.get_create_scene_use_case)]
ListScenesDep = Annotated[ListScenes, Depends(container.get_list_scenes_use_case)]
GetSceneDep = Annotated[GetScene, Depends(container.get_get_scene_use_case)]
UpdateSceneDep = Annotated[UpdateScene, Depends(container.get_update_scene_use_case)]
MoveSceneDep = Annotated[MoveScene, Depends(container.get_move_scene_use_case)]
DeleteSceneDep = Annotated[DeleteScene, Depends(container.get_delete_scene_use_case)]

# ---- Use-case dependencies (Slice α5d.1 — Project Versions) -----------

CreateProjectVersionDep = Annotated[
    CreateProjectVersion, Depends(container.get_create_project_version_use_case)
]
ListProjectVersionsDep = Annotated[
    ListProjectVersions, Depends(container.get_list_project_versions_use_case)
]
GetProjectVersionDep = Annotated[
    GetProjectVersion, Depends(container.get_get_project_version_use_case)
]

# ---- Use-case dependencies (Slice α5d.2 — restore + diff) -------------

RestoreProjectVersionDep = Annotated[
    RestoreProjectVersion, Depends(container.get_restore_project_version_use_case)
]
DiffProjectVersionsDep = Annotated[
    DiffProjectVersions, Depends(container.get_diff_project_versions_use_case)
]

# ---- Use-case dependencies (Slice α5d.3 — branch / fork) --------------

BranchProjectVersionDep = Annotated[
    BranchProjectVersion, Depends(container.get_branch_project_version_use_case)
]

# ---- Use-case dependencies (Slice α6.1 — Prompts) ---------------------

CreatePromptDep = Annotated[CreatePrompt, Depends(container.get_create_prompt_use_case)]
ListPromptsDep = Annotated[ListPrompts, Depends(container.get_list_prompts_use_case)]
GetPromptDep = Annotated[GetPrompt, Depends(container.get_get_prompt_use_case)]
UpdatePromptDep = Annotated[UpdatePrompt, Depends(container.get_update_prompt_use_case)]
DeletePromptDep = Annotated[DeletePrompt, Depends(container.get_delete_prompt_use_case)]

# ---- Use-case dependencies (Slice α6.2 — Media) -----------------------

RegisterMediaDep = Annotated[RegisterMedia, Depends(container.get_register_media_use_case)]
ListMediaDep = Annotated[ListMedia, Depends(container.get_list_media_use_case)]
GetMediaDep = Annotated[GetMedia, Depends(container.get_get_media_use_case)]
UpdateMediaDep = Annotated[UpdateMedia, Depends(container.get_update_media_use_case)]
DeleteMediaDep = Annotated[DeleteMedia, Depends(container.get_delete_media_use_case)]


def _bearer_access_token(authorization: str | None = Header(default=None)) -> str:
    """Extract the raw access token from an ``Authorization: Bearer …`` header.

    Raises ``UnauthorizedError`` (401 via the app error handler) for a
    missing / malformed header. Cryptographic validation is the
    downstream use case's job — this dependency only parses the header
    shape.

    Retained for α2b's ``/auth/logout`` endpoint, which is intentionally
    lenient about token expiry and therefore has its own message shape.
    α3's ``get_current_user`` inlines the same parse-then-verify logic
    with anti-enumeration generic messaging (see :data:`_GENERIC_401`).
    """
    if not authorization:
        raise UnauthorizedError("missing authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("malformed authorization header")
    return token


BearerAccessTokenDep = Annotated[str, Depends(_bearer_access_token)]


# ---- Authenticated-request seam (Slice α3) ----------------------------


def _reject(
    reason: str,
    *,
    security_event: bool = False,
    **fields: object,
) -> UnauthorizedError:
    """Emit ``auth.request.rejected`` with the specific reason and return the generic 401.

    Centralising this keeps the ``get_current_user`` body flat and
    guarantees every rejection path uses the same client-facing message
    (:data:`_GENERIC_401`) — an anti-enumeration invariant asserted by
    pre-flight §4.1 A2–A9.
    """
    _LOGGER.warning(
        "auth.request.rejected",
        reason=reason,
        security_event=security_event,
        **fields,
    )
    return UnauthorizedError(_GENERIC_401)


async def get_current_user(
    request: Request,
    token_issuer: TokenIssuerDep,
    uow: UoWDep,
) -> User:
    """Resolve a bearer access token into a live ``User`` domain entity.

    Every non-happy branch raises :class:`UnauthorizedError` with the
    same message (:data:`_GENERIC_401`); the specific reason is only
    visible in the server-side ``auth.request.rejected`` structured log.
    ``security_event=True`` is set on tamper-flavoured reasons
    (``verify_failed`` for signature/kind mismatches; ``sid_missing_session``)
    — signature failures caused by mere expiry are noisy under normal
    client behaviour and are NOT flagged (matches α2b logout / refresh
    discipline; pre-flight §2.D4).

    Note the design choice: header parsing is inlined here rather than
    reusing :func:`_bearer_access_token` so all seven reason branches
    can log through :func:`_reject` uniformly. ``_bearer_access_token``
    is preserved for ``/auth/logout``, which has its own message shape
    (α2b, deliberate).
    """
    ip = client_ip(request)
    user_agent = request.headers.get("user-agent")

    # ---- Step 1: header shape (reason=missing_header / malformed_header)
    authorization = request.headers.get("authorization")
    if not authorization:
        raise _reject("missing_header", ip=ip)
    scheme, _, raw_token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw_token:
        raise _reject("malformed_header", ip=ip)

    # ---- Step 2: verify JWT (reason=verify_failed).
    #
    # Signature / kind / claim-shape failures set ``security_event=True``
    # because they can only be produced by tampering. Expiry does NOT
    # set the flag: an expired access token is the routine "you should
    # refresh now" signal, produced by every long-lived client. The
    # ``AuthTokenIssuer`` collapses all of these into a single
    # ``UnauthorizedError`` today; distinguishing at the log layer
    # requires inspecting the exception message, which is a fragile
    # coupling. For α3 we mark all verify_failed cases with
    # ``security_event=True`` and accept the noise cost. If SIEM
    # tuning proves this too noisy, promote ``AuthTokenIssuer.verify_access``
    # to raise sub-typed errors (its own slice).
    try:
        claims = token_issuer.verify_access(raw_token)
    except UnauthorizedError as e:
        raise _reject("verify_failed", security_event=True, detail=str(e), ip=ip) from e

    # ---- Step 3: session liveness (reason=sid_missing_session / session_revoked / session_expired).
    now = container.get_clock().now()
    async with uow:
        row = await uow.sessions.get_by_id(claims.session_id)
        if row is None:
            raise _reject(
                "sid_missing_session",
                security_event=True,
                claimed_sid=str(claims.session_id),
                user_id=str(claims.subject),
                ip=ip,
            )
        if row.revoked_at is not None:
            raise _reject(
                "session_revoked",
                session_id=str(row.id),
                user_id=str(row.user_id),
                ip=ip,
            )
        if row.expires_at <= now:
            raise _reject(
                "session_expired",
                session_id=str(row.id),
                user_id=str(row.user_id),
                ip=ip,
            )

        # ---- Step 4: user liveness (reason=sid_user_gone).
        user = await uow.users.get_by_id(row.user_id)
        if user is None:
            raise _reject(
                "sid_user_gone",
                session_id=str(row.id),
                user_id=str(row.user_id),
                ip=ip,
            )

    # ---- Happy path.
    _LOGGER.info(
        "auth.request.authenticated",
        user_id=str(user.id),
        session_id=str(row.id),
        ip=ip,
        user_agent=user_agent,
    )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
