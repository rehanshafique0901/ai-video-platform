"""``/api/v1/auth/*`` HTTP router.

The router is deliberately thin. It:

1. Deserialises the request DTO (Pydantic validates + lowercases the
   email before we see it).
2. Reads the caller's IP + user-agent for audit persistence.
3. Delegates to the use case injected via the deps aliases (all wired
   through ``app.core.container`` — see ``app.api.v1.deps``).
4. Maps the use-case result into the wire DTO and wraps it in the
   API_CONTRACT §1.1 envelope.
5. Sets the HTTP status code (201 for register, 200 for login/refresh,
   204 for logout).

Errors raised by the use case (all ``ApplicationError`` subclasses)
are caught by the FastAPI exception handlers registered in
``app.core.errors`` and translated to the standard error envelope.
The router itself contains no try / except.

Endpoint inventory:

* α2a: ``POST /register`` (201), ``POST /login`` (200)
* α2b: ``POST /refresh`` (200), ``POST /logout`` (204)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    BearerAccessTokenDep,
    LoginUserDep,
    LogoutSessionDep,
    RefreshSessionDep,
    RegisterUserDep,
)
from app.api.v1.schemas.auth import (
    AuthTokensPayload,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
)
from app.api.v1.schemas.users import UserPublic
from app.application.use_cases.auth.login_user import LoginUserResult
from app.application.use_cases.auth.refresh_session import RefreshSessionResult
from app.application.use_cases.auth.register_user import RegisterUserResult

router = APIRouter(prefix="/auth", tags=["auth"])


def _envelope(payload: AuthTokensPayload, request: Request) -> dict[str, Any]:
    request_id = getattr(request.state, "request_id", "")
    return {"data": payload.model_dump(mode="json"), "meta": {"request_id": request_id}}


def _to_payload(user_id: Any, tokens_bundle: Any, user: Any) -> AuthTokensPayload:
    """Adapt a use-case result into the wire DTO.

    α4: :class:`UserPublic` gained ``updated_at`` + ``version`` (see
    ``schemas/users.py`` module docstring). Both are additive additions
    to the existing register / login / refresh response bodies —
    clients that ignored unknown fields (per JSON API best practice)
    are unaffected. The values give the caller everything they need
    to issue a subsequent ``PATCH /users/me`` without a preliminary
    ``GET /users/me`` round-trip.
    """
    return AuthTokensPayload(
        user=UserPublic(
            id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            display_name=user.display_name,
            email_verified_at=user.email_verified_at,
            created_at=user.created_at,
            updated_at=user.updated_at,
            version=user.version,
        ),
        access_token=tokens_bundle.access_token,
        refresh_token=tokens_bundle.refresh_token,
    )


@router.post("/register")
async def register(
    body: RegisterRequest,
    request: Request,
    use_case: RegisterUserDep,
) -> JSONResponse:
    result: RegisterUserResult = await use_case.execute(
        email=body.email,
        password=body.password,
        name=body.name,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    payload = _to_payload(result.user.id, result.tokens, result.user)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=_envelope(payload, request),
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    use_case: LoginUserDep,
) -> JSONResponse:
    result: LoginUserResult = await use_case.execute(
        email=body.email,
        password=body.password,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    payload = _to_payload(result.user.id, result.tokens, result.user)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_envelope(payload, request),
    )


@router.post("/refresh")
async def refresh(
    body: RefreshRequest,
    request: Request,
    use_case: RefreshSessionDep,
) -> JSONResponse:
    result: RefreshSessionResult = await use_case.execute(
        refresh_token=body.refresh_token,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    payload = _to_payload(result.user.id, result.tokens, result.user)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_envelope(payload, request),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    access_token: BearerAccessTokenDep,
    use_case: LogoutSessionDep,
) -> Response:
    """Terminate the session identified by the bearer access token.

    Accepts an *expired* access token as a valid credential; a user
    whose access token just expired must still be able to log out
    without first refreshing. See ``LogoutSession`` docstring and
    ``docs/engineering/AUTH_TOKEN_LIFECYCLE.md`` §Logout.

    Idempotent: second and subsequent calls with the same ``sid`` also
    return 204. The response body is empty by contract; no envelope
    is emitted for 204 (API_CONTRACT §1.1 — envelopes accompany a
    JSON payload only).
    """
    await use_case.execute(access_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _client_ip(request: Request) -> str | None:
    """Return the caller IP, honouring a common reverse-proxy header if present.

    Storing IP is best-effort for audit: TrustedHost + X-Forwarded-For
    handling for production land in a later slice (behind an ADR). For
    α2a we take the first value of ``X-Forwarded-For`` if set, otherwise
    ``request.client.host``.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.client.host if request.client else None
