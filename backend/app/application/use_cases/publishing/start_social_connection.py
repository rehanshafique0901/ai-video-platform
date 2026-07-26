"""``StartSocialConnection`` — begin an OAuth connection to a destination (α8.6a).

Stateless: builds a signed, short-lived ``state`` token (CSRF + acting principal) and
returns the provider authorization URL the user is redirected to. No credential is created
until the callback completes the exchange.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.application.interfaces.oauth_state_signer import ConnectionState, IOAuthStateSigner
from app.application.interfaces.social_oauth_client import ISocialOAuthClient
from app.core.errors import ValidationFailedError
from app.domain.identity.user import User


class StartSocialConnection:
    """Produce a provider authorization URL bound to a signed connection state."""

    def __init__(
        self,
        *,
        oauth_clients: Mapping[str, ISocialOAuthClient],
        state_signer: IOAuthStateSigner,
        redirect_uri: str,
    ) -> None:
        self._oauth_clients = oauth_clients
        self._state_signer = state_signer
        self._redirect_uri = redirect_uri

    async def execute(self, *, user: User, platform: str) -> str:
        client = self._oauth_clients.get(platform)
        if client is None:
            raise ValidationFailedError(
                "unsupported destination platform",
                details={"platform": platform},
            )
        state = self._state_signer.sign(
            ConnectionState(user_id=user.id, tenant_id=user.tenant_id, platform=platform)
        )
        return client.authorization_url(state=state, redirect_uri=self._redirect_uri)


__all__ = ["StartSocialConnection"]
