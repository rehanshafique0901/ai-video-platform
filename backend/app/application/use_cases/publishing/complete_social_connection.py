"""``CompleteSocialConnection`` — finish an OAuth connection at the callback (α8.6a).

Verifies the signed ``state`` (CSRF + acting principal), exchanges the authorization code
for tokens, upserts the ``SocialAccount`` profile (committed), then hands the tokens to the
credential store, which envelope-encrypts them. The tokens never reach the wire or the log;
the returned :class:`SocialAccount` carries only non-secret profile.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.application.interfaces.oauth_state_signer import (
    InvalidConnectionStateError,
    IOAuthStateSigner,
)
from app.application.interfaces.social_credential_store import ISocialCredentialStore
from app.application.interfaces.social_oauth_client import (
    ISocialOAuthClient,
    OAuthExchangeError,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ValidationFailedError
from app.domain.publishing.social_account import SocialAccount


class CompleteSocialConnection:
    """Exchange an OAuth code and persist the connected account + encrypted credential."""

    def __init__(
        self,
        *,
        uow: IUnitOfWork,
        oauth_clients: Mapping[str, ISocialOAuthClient],
        state_signer: IOAuthStateSigner,
        credential_store: ISocialCredentialStore,
        redirect_uri: str,
    ) -> None:
        self._uow = uow
        self._oauth_clients = oauth_clients
        self._state_signer = state_signer
        self._credential_store = credential_store
        self._redirect_uri = redirect_uri

    async def execute(self, *, state_token: str, code: str) -> SocialAccount:
        try:
            state = self._state_signer.verify(state_token)
        except InvalidConnectionStateError as e:
            raise ValidationFailedError("invalid or expired connection state") from e

        client = self._oauth_clients.get(state.platform)
        if client is None:
            raise ValidationFailedError(
                "unsupported destination platform", details={"platform": state.platform}
            )

        try:
            grant = await client.exchange_code(code=code, redirect_uri=self._redirect_uri)
        except OAuthExchangeError as e:
            raise ValidationFailedError("authorization code exchange failed") from e

        async with self._uow:
            account = await self._uow.social_accounts.upsert_connected(
                tenant_id=state.tenant_id,
                user_id=state.user_id,
                platform=state.platform,
                external_account_id=grant.external_account_id,
                display_name=grant.display_name,
                scopes=grant.tokens.scopes,
            )
            await self._uow.commit()

        # Store the encrypted credential after the account row is committed (the credential
        # references it). A failure here leaves the account with no credential → authorize()
        # fails closed until the user reconnects.
        await self._credential_store.store(account.id, grant.tokens)
        return account


__all__ = ["CompleteSocialConnection"]
