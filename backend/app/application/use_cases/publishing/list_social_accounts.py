"""``ListSocialAccounts`` — the caller's connected destination accounts (α8.6a).

Read-only, owner-scoped. Returns non-secret profile only; the credential context is never
touched.
"""

from __future__ import annotations

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.domain.identity.user import User
from app.domain.publishing.social_account import SocialAccount


class ListSocialAccounts:
    """List the caller's social accounts (newest first)."""

    def __init__(self, *, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, user: User) -> list[SocialAccount]:
        async with self._uow:
            return await self._uow.social_accounts.list_for_owner(
                tenant_id=user.tenant_id, user_id=user.id
            )


__all__ = ["ListSocialAccounts"]
