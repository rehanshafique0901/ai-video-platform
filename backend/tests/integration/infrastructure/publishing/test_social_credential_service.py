"""Integration tests for ``SocialCredentialService`` (α8.6a, ADR-0047).

Exercises the credential boundary end-to-end against the live database: envelope-encrypt on
store, decrypt + proactive refresh on authorize, and delete on revoke — plus the fail-closed
paths. A self-managed connection with a rolled-back top-level transaction keeps the shared
database clean (the service opens its own sessions on this connection via a
``create_savepoint`` sessionmaker).

Critically asserts **no plaintext token ever lands in the database** (C1/C2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.clock import IClock
from app.application.interfaces.social_credential_store import (
    CredentialUnavailableError,
    GrantedTokens,
)
from app.domain.publishing.social_account import AccountStatus
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.publishing import (
    SocialAccount as SocialAccountRow,
    SocialCredential as SocialCredentialRow,
)
from app.infrastructure.publishing.credentials.credential_service import SocialCredentialService
from app.infrastructure.publishing.credentials.envelope import EnvelopeCipher
from app.infrastructure.publishing.credentials.master_key import EnvMasterKeyProvider
from app.infrastructure.publishing.oauth.mock_oauth_client import MockSocialOAuthClient

pytestmark = pytest.mark.integration


class _StubClock(IClock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _cipher() -> EnvelopeCipher:
    return EnvelopeCipher(EnvMasterKeyProvider(version="v1", secret="integration-master-key"))


@pytest_asyncio.fixture
async def bound(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.connect() as connection:
        outer_tx = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield factory
        finally:
            await outer_tx.rollback()


async def _seed_account(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: AccountStatus = AccountStatus.CONNECTED,
) -> UUID:
    tenant_id = uuid4()
    user_id = uuid4()
    account_id = uuid4()
    async with factory() as session:
        await session.execute(
            insert(Tenant).values(id=tenant_id, name="Cred", slug=f"cred-{tenant_id}")
        )
        await session.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"cred-{user_id}@example.com",
                display_name="Cred Owner",
            )
        )
        await session.execute(
            insert(SocialAccountRow).values(
                id=account_id,
                tenant_id=tenant_id,
                user_id=user_id,
                platform="mock",
                external_account_id=f"ext-{account_id}",
                display_name="Mock Channel",
                status=status.value,
                scopes=["publish"],
                connected_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return account_id


def _service(factory: async_sessionmaker[AsyncSession], clock: IClock) -> SocialCredentialService:
    return SocialCredentialService(
        session_factory=factory,
        cipher=_cipher(),
        oauth_clients={"mock": MockSocialOAuthClient(clock=clock)},
        clock=clock,
    )


async def test_store_then_authorize_returns_access_token(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    clock = _StubClock(datetime(2026, 7, 26, 12, 0, tzinfo=UTC))
    account_id = await _seed_account(bound)
    service = _service(bound, clock)

    await service.store(
        account_id,
        GrantedTokens(
            access_token="live-access-token",
            refresh_token="live-refresh-token",
            expires_at=clock.now() + timedelta(hours=1),
            scopes=("publish",),
        ),
    )
    ctx = await service.authorize(account_id)

    assert ctx.access_token == "live-access-token"
    assert ctx.scopes == ("publish",)


async def test_stored_credential_has_no_plaintext_token(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    clock = _StubClock(datetime(2026, 7, 26, 12, 0, tzinfo=UTC))
    account_id = await _seed_account(bound)
    service = _service(bound, clock)
    await service.store(
        account_id,
        GrantedTokens(
            access_token="PLAINTEXT-ACCESS",
            refresh_token="PLAINTEXT-REFRESH",
            expires_at=clock.now() + timedelta(hours=1),
            scopes=("publish",),
        ),
    )

    async with bound() as session:
        row = (
            await session.execute(
                select(SocialCredentialRow).where(
                    SocialCredentialRow.social_account_id == account_id
                )
            )
        ).scalar_one()

    assert b"PLAINTEXT-ACCESS" not in row.ciphertext
    assert b"PLAINTEXT-REFRESH" not in row.ciphertext
    assert b"PLAINTEXT" not in row.wrapped_dek
    assert row.key_version == "v1"
    assert row.algorithm == "AES-256-GCM"
    assert row.access_token_expires_at is not None


async def test_authorize_refreshes_when_near_expiry(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    clock = _StubClock(datetime(2026, 7, 26, 12, 0, tzinfo=UTC))
    account_id = await _seed_account(bound)
    service = _service(bound, clock)
    # Stored token already expired → authorize must refresh via the mock OAuth client.
    await service.store(
        account_id,
        GrantedTokens(
            access_token="stale-access",
            refresh_token="mock-refresh-CODE",
            expires_at=clock.now() - timedelta(seconds=1),
            scopes=("publish",),
        ),
    )

    ctx = await service.authorize(account_id)

    assert ctx.access_token == "mock-access-refreshed-mock-refresh-CODE"


async def test_authorize_on_revoked_account_fails_closed(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    clock = _StubClock(datetime(2026, 7, 26, 12, 0, tzinfo=UTC))
    account_id = await _seed_account(bound, status=AccountStatus.REVOKED)
    service = _service(bound, clock)
    await service.store(
        account_id,
        GrantedTokens(
            access_token="x",
            refresh_token="y",
            expires_at=clock.now() + timedelta(hours=1),
            scopes=("publish",),
        ),
    )
    with pytest.raises(CredentialUnavailableError):
        await service.authorize(account_id)


async def test_authorize_without_stored_credential_fails_closed(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    clock = _StubClock(datetime(2026, 7, 26, 12, 0, tzinfo=UTC))
    account_id = await _seed_account(bound)
    service = _service(bound, clock)
    with pytest.raises(CredentialUnavailableError):
        await service.authorize(account_id)


async def test_revoke_deletes_credential_and_authorize_then_fails(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    clock = _StubClock(datetime(2026, 7, 26, 12, 0, tzinfo=UTC))
    account_id = await _seed_account(bound)
    service = _service(bound, clock)
    await service.store(
        account_id,
        GrantedTokens(
            access_token="a",
            refresh_token="r",
            expires_at=clock.now() + timedelta(hours=1),
            scopes=("publish",),
        ),
    )

    await service.revoke(account_id)

    async with bound() as session:
        row = (
            await session.execute(
                select(SocialCredentialRow).where(
                    SocialCredentialRow.social_account_id == account_id
                )
            )
        ).scalar_one_or_none()
    assert row is None
    with pytest.raises(CredentialUnavailableError):
        await service.authorize(account_id)
