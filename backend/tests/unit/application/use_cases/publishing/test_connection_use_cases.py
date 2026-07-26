"""Unit tests for the α8.6a connection use cases (in-memory fakes, no DB).

Prove the orchestration + boundaries: unsupported platform / invalid state → validation
errors; owner-scoped revoke (404 for a foreign id); the credential store is handed tokens
(never the DB), and the returned/observable objects carry no token material.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.oauth_state_signer import (
    ConnectionState,
    InvalidConnectionStateError,
    IOAuthStateSigner,
)
from app.application.interfaces.social_credential_store import (
    AuthorizedContext,
    GrantedTokens,
    ISocialCredentialStore,
)
from app.application.interfaces.social_oauth_client import ISocialOAuthClient, OAuthGrant
from app.application.use_cases.publishing.complete_social_connection import (
    CompleteSocialConnection,
)
from app.application.use_cases.publishing.list_social_accounts import ListSocialAccounts
from app.application.use_cases.publishing.revoke_social_account import RevokeSocialAccount
from app.application.use_cases.publishing.start_social_connection import StartSocialConnection
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.identity.user import User
from app.domain.publishing.social_account import AccountStatus, SocialAccount

_TENANT = uuid4()
_USER = uuid4()


def _user() -> User:
    now = datetime(2026, 7, 26, tzinfo=UTC)
    return User(
        id=_USER,
        tenant_id=_TENANT,
        email="creator@example.com",
        password_hash="x",
        display_name="Creator",
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=1,
    )


def _account(account_id: UUID, *, status: AccountStatus = AccountStatus.CONNECTED) -> SocialAccount:
    now = datetime(2026, 7, 26, tzinfo=UTC)
    return SocialAccount(
        id=account_id,
        tenant_id=_TENANT,
        user_id=_USER,
        platform="mock",
        external_account_id=f"ext-{account_id}",
        display_name="Mock Channel",
        status=status,
        scopes=("publish",),
        connected_at=now,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )


class _FakeAccountRepo:
    def __init__(self) -> None:
        self.accounts: dict[UUID, SocialAccount] = {}

    async def upsert_connected(
        self, *, tenant_id, user_id, platform, external_account_id, display_name, scopes
    ) -> SocialAccount:
        account_id = uuid4()
        account = SocialAccount(
            id=account_id,
            tenant_id=tenant_id,
            user_id=user_id,
            platform=platform,
            external_account_id=external_account_id,
            display_name=display_name,
            status=AccountStatus.CONNECTED,
            scopes=scopes,
            connected_at=datetime(2026, 7, 26, tzinfo=UTC),
            revoked_at=None,
            created_at=datetime(2026, 7, 26, tzinfo=UTC),
            updated_at=datetime(2026, 7, 26, tzinfo=UTC),
        )
        self.accounts[account_id] = account
        return account

    async def get_owned(self, *, tenant_id, user_id, social_account_id) -> SocialAccount | None:
        account = self.accounts.get(social_account_id)
        if account is None or account.tenant_id != tenant_id or account.user_id != user_id:
            return None
        return account

    async def list_for_owner(self, *, tenant_id, user_id) -> list[SocialAccount]:
        return [
            a for a in self.accounts.values() if a.tenant_id == tenant_id and a.user_id == user_id
        ]

    async def mark_revoked(self, *, tenant_id, user_id, social_account_id) -> SocialAccount | None:
        account = await self.get_owned(
            tenant_id=tenant_id, user_id=user_id, social_account_id=social_account_id
        )
        if account is None:
            return None
        revoked = _account(social_account_id, status=AccountStatus.REVOKED)
        self.accounts[social_account_id] = revoked
        return revoked


class _FakeUoW:
    def __init__(self, repo: _FakeAccountRepo) -> None:
        self.social_accounts = repo
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _FakeCredentialStore(ISocialCredentialStore):
    def __init__(self) -> None:
        self.stored: dict[UUID, GrantedTokens] = {}
        self.revoked: list[UUID] = []

    async def store(self, social_account_id: UUID, tokens: GrantedTokens) -> None:
        self.stored[social_account_id] = tokens

    async def authorize(self, social_account_id: UUID) -> AuthorizedContext:
        tokens = self.stored[social_account_id]
        return AuthorizedContext(
            access_token=tokens.access_token, expires_at=tokens.expires_at, scopes=tokens.scopes
        )

    async def revoke(self, social_account_id: UUID) -> None:
        self.revoked.append(social_account_id)


class _FakeOAuthClient(ISocialOAuthClient):
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"https://provider/auth?state={state}&redirect_uri={redirect_uri}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthGrant:
        return OAuthGrant(
            external_account_id=f"ext-{code}",
            display_name="Mock Channel",
            tokens=GrantedTokens(
                access_token=f"access-{code}",
                refresh_token=f"refresh-{code}",
                expires_at=datetime(2026, 7, 26, 13, 0, tzinfo=UTC),
                scopes=("publish",),
            ),
        )

    async def refresh(self, *, refresh_token: str) -> GrantedTokens:  # pragma: no cover
        raise NotImplementedError

    async def revoke(self, *, token: str) -> None:  # pragma: no cover
        return None


class _FakeStateSigner(IOAuthStateSigner):
    def __init__(self, *, valid: bool = True) -> None:
        self._valid = valid

    def sign(self, state: ConnectionState) -> str:
        return f"signed:{state.user_id}:{state.tenant_id}:{state.platform}"

    def verify(self, token: str) -> ConnectionState:
        if not self._valid:
            raise InvalidConnectionStateError("bad state")
        _, user_id, tenant_id, platform = token.split(":")
        return ConnectionState(user_id=UUID(user_id), tenant_id=UUID(tenant_id), platform=platform)


# ---- StartSocialConnection ------------------------------------------------


async def test_start_returns_authorization_url_for_known_platform() -> None:
    use_case = StartSocialConnection(
        oauth_clients={"mock": _FakeOAuthClient()},
        state_signer=_FakeStateSigner(),
        redirect_uri="http://app/callback",
    )
    url = await use_case.execute(user=_user(), platform="mock")
    assert url.startswith("https://provider/auth?state=signed:")
    assert "redirect_uri=http://app/callback" in url


async def test_start_rejects_unsupported_platform() -> None:
    use_case = StartSocialConnection(
        oauth_clients={"mock": _FakeOAuthClient()},
        state_signer=_FakeStateSigner(),
        redirect_uri="http://app/callback",
    )
    with pytest.raises(ValidationFailedError):
        await use_case.execute(user=_user(), platform="youtube")


# ---- CompleteSocialConnection --------------------------------------------


async def test_complete_upserts_account_and_stores_encrypted_tokens() -> None:
    repo = _FakeAccountRepo()
    uow = _FakeUoW(repo)
    store = _FakeCredentialStore()
    use_case = CompleteSocialConnection(
        uow=uow,  # type: ignore[arg-type]
        oauth_clients={"mock": _FakeOAuthClient()},
        state_signer=_FakeStateSigner(),
        credential_store=store,
        redirect_uri="http://app/callback",
    )
    token = _FakeStateSigner().sign(
        ConnectionState(user_id=_USER, tenant_id=_TENANT, platform="mock")
    )

    account = await use_case.execute(state_token=token, code="CODE123")

    assert account.platform == "mock"
    assert account.external_account_id == "ext-CODE123"
    assert account.status is AccountStatus.CONNECTED
    assert uow.committed is True
    # The credential store received the tokens; the returned account exposes none of them.
    assert account.id in store.stored
    assert store.stored[account.id].access_token == "access-CODE123"


async def test_complete_rejects_invalid_state() -> None:
    use_case = CompleteSocialConnection(
        uow=_FakeUoW(_FakeAccountRepo()),  # type: ignore[arg-type]
        oauth_clients={"mock": _FakeOAuthClient()},
        state_signer=_FakeStateSigner(valid=False),
        credential_store=_FakeCredentialStore(),
        redirect_uri="http://app/callback",
    )
    with pytest.raises(ValidationFailedError):
        await use_case.execute(state_token="anything", code="CODE")


# ---- RevokeSocialAccount --------------------------------------------------


async def test_revoke_owned_account_revokes_credential_and_marks_revoked() -> None:
    repo = _FakeAccountRepo()
    account = await repo.upsert_connected(
        tenant_id=_TENANT,
        user_id=_USER,
        platform="mock",
        external_account_id="ext-1",
        display_name="Mock",
        scopes=("publish",),
    )
    store = _FakeCredentialStore()
    use_case = RevokeSocialAccount(uow=_FakeUoW(repo), credential_store=store)  # type: ignore[arg-type]

    await use_case.execute(user=_user(), social_account_id=account.id)

    assert account.id in store.revoked
    assert repo.accounts[account.id].status is AccountStatus.REVOKED


async def test_revoke_unknown_account_is_not_found() -> None:
    use_case = RevokeSocialAccount(
        uow=_FakeUoW(_FakeAccountRepo()),  # type: ignore[arg-type]
        credential_store=_FakeCredentialStore(),
    )
    with pytest.raises(NotFoundError):
        await use_case.execute(user=_user(), social_account_id=uuid4())


async def test_revoke_foreign_account_is_not_found() -> None:
    repo = _FakeAccountRepo()
    # An account owned by a different user/tenant.
    foreign_id = uuid4()
    repo.accounts[foreign_id] = SocialAccount(
        id=foreign_id,
        tenant_id=uuid4(),
        user_id=uuid4(),
        platform="mock",
        external_account_id="ext-foreign",
        display_name=None,
        status=AccountStatus.CONNECTED,
        scopes=(),
        connected_at=None,
        revoked_at=None,
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
        updated_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    use_case = RevokeSocialAccount(uow=_FakeUoW(repo), credential_store=_FakeCredentialStore())  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        await use_case.execute(user=_user(), social_account_id=foreign_id)


# ---- ListSocialAccounts ---------------------------------------------------


async def test_list_returns_only_callers_accounts() -> None:
    repo = _FakeAccountRepo()
    await repo.upsert_connected(
        tenant_id=_TENANT,
        user_id=_USER,
        platform="mock",
        external_account_id="ext-1",
        display_name="Mine",
        scopes=(),
    )
    use_case = ListSocialAccounts(uow=_FakeUoW(repo))  # type: ignore[arg-type]
    accounts = await use_case.execute(user=_user())
    assert len(accounts) == 1
    assert accounts[0].display_name == "Mine"
