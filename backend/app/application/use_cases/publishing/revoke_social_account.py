"""``RevokeSocialAccount`` — disconnect a destination account (α8.6a).

Owner-scoped: establishes the caller owns the account (uniform 404 otherwise), invalidates
the credential at the provider + deletes it locally (C6), then marks the account
``revoked``. After this, :meth:`ISocialCredentialStore.authorize` fails closed for the
account — a disconnected account cannot publish.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.social_credential_store import ISocialCredentialStore
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.identity.user import User


class RevokeSocialAccount:
    """Revoke one of the caller's connected destination accounts."""

    def __init__(
        self,
        *,
        uow: IUnitOfWork,
        credential_store: ISocialCredentialStore,
    ) -> None:
        self._uow = uow
        self._credential_store = credential_store

    async def execute(self, *, user: User, social_account_id: UUID) -> None:
        async with self._uow:
            account = await self._uow.social_accounts.get_owned(
                tenant_id=user.tenant_id,
                user_id=user.id,
                social_account_id=social_account_id,
            )
        if account is None:
            raise NotFoundError("social account not found")

        await self._credential_store.revoke(social_account_id)

        async with self._uow:
            await self._uow.social_accounts.mark_revoked(
                tenant_id=user.tenant_id,
                user_id=user.id,
                social_account_id=social_account_id,
            )
            await self._uow.commit()


__all__ = ["RevokeSocialAccount"]
