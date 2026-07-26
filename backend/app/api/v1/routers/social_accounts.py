"""``/api/v1/social-accounts/*`` HTTP router (α8.6a Account Connections).

The creator-workflow connection lifecycle for external destinations:

* ``POST /social-accounts/connect``        → 200, returns the provider authorization URL
  (bearer-authenticated; body ``{platform}``).
* ``GET  /social-accounts/callback``       → 200, the OAuth redirect target — authenticated
  by the signed ``state`` (no bearer), exchanges the code, encrypts + stores the credential,
  and returns the connected account (**no tokens** in the body — C8).
* ``GET  /social-accounts``                → 200, list the caller's connected accounts.
* ``POST /social-accounts/{id}/revoke``    → 204, disconnect one account (owner-scoped;
  404 if missing / not the caller's — anti-enumeration).

The router stays thin: DTO projection + envelope. Ownership scoping, CSRF/state
verification, OAuth exchange, and the credential/encryption boundary all live in the use
cases + infrastructure. Errors (``NotFoundError`` → 404, ``ValidationFailedError`` → 422)
are rendered by ``app.core.errors``; this module contains no try / except.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    CompleteSocialConnectionDep,
    CurrentUserDep,
    ListSocialAccountsDep,
    RevokeSocialAccountDep,
    StartSocialConnectionDep,
)
from app.api.v1.helpers import envelope
from app.api.v1.schemas.social_accounts import (
    ConnectSocialAccountRequest,
    ConnectSocialAccountResponse,
    SocialAccountPublic,
)
from app.domain.publishing.social_account import SocialAccount

router = APIRouter(prefix="/social-accounts", tags=["social-accounts"])


def _to_public(account: SocialAccount) -> SocialAccountPublic:
    """Project a domain ``SocialAccount`` into the wire DTO (non-secret profile only)."""
    return SocialAccountPublic(
        id=account.id,
        user_id=account.user_id,
        platform=account.platform,
        external_account_id=account.external_account_id,
        display_name=account.display_name,
        status=account.status,
        scopes=list(account.scopes),
        connected_at=account.connected_at,
        revoked_at=account.revoked_at,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.post("/connect")
async def connect(
    body: ConnectSocialAccountRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: StartSocialConnectionDep,
) -> JSONResponse:
    """Begin connecting a destination account; return the provider authorization URL.

    An unsupported ``platform`` is a 422. The returned URL carries a signed, short-lived
    ``state`` token (CSRF) identifying the caller for the callback.
    """
    authorization_url = await use_case.execute(user=current_user, platform=body.platform)
    return JSONResponse(
        content=envelope(ConnectSocialAccountResponse(authorization_url=authorization_url), request)
    )


@router.get("/callback")
async def callback(
    request: Request,
    use_case: CompleteSocialConnectionDep,
    state: str = Query(min_length=1),
    code: str = Query(min_length=1),
) -> JSONResponse:
    """OAuth redirect target: verify ``state``, exchange ``code``, store the credential.

    Authenticated by the signed ``state`` (the acting user + tenant travel inside it) — a
    forged / expired / tampered state is a 422. The response carries the connected account's
    non-secret profile only; the OAuth tokens never reach the wire (C8).
    """
    account = await use_case.execute(state_token=state, code=code)
    return JSONResponse(content=envelope(_to_public(account), request))


@router.get("")
async def list_accounts(
    request: Request,
    current_user: CurrentUserDep,
    use_case: ListSocialAccountsDep,
) -> JSONResponse:
    """List the caller's connected destination accounts (newest first)."""
    accounts = await use_case.execute(user=current_user)
    return JSONResponse(content=envelope([_to_public(a) for a in accounts], request))


@router.post("/{social_account_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(
    social_account_id: UUID,
    current_user: CurrentUserDep,
    use_case: RevokeSocialAccountDep,
) -> Response:
    """Disconnect one of the caller's accounts (idempotent). 404 if missing / not owned.

    Invalidates the credential at the provider + deletes it locally, then marks the account
    ``revoked`` — after which the account can no longer yield an authorized context.
    """
    await use_case.execute(user=current_user, social_account_id=social_account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
