"""``SocialCredentialService`` — the credential-ownership boundary (ADR-0047 C3/C7).

The *only* module that decrypts stored OAuth tokens. Implements
:class:`ISocialCredentialStore`: callers request authorized access for a ``SocialAccount``
and receive an :class:`AuthorizedContext` (a short-lived bearer, no refresh token, no key
material). Persistence uses its own short-lived sessions (like
``SqlExecutionRuntimeStore``) so the port stays free of SQLAlchemy; the account row is
written+committed by the connect use case before :meth:`store` runs.

Fail-closed: :meth:`authorize` raises :class:`CredentialUnavailableError` when the account
is not ``connected``, has no stored credential, or is expired and cannot be refreshed —
never a plaintext fallback, never a silent degrade.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.clock import IClock
from app.application.interfaces.social_credential_store import (
    AuthorizedContext,
    CredentialUnavailableError,
    GrantedTokens,
    ISocialCredentialStore,
)
from app.application.interfaces.social_oauth_client import ISocialOAuthClient, OAuthExchangeError
from app.domain.publishing.social_account import AccountStatus
from app.infrastructure.db.models.publishing import (
    SocialAccount as SocialAccountRow,
    SocialCredential as SocialCredentialRow,
)
from app.infrastructure.publishing.credentials.envelope import EncryptedBlob, EnvelopeCipher


class SocialCredentialService(ISocialCredentialStore):
    """Own the encrypt / refresh / revoke lifecycle of a connected account's credential."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: EnvelopeCipher,
        oauth_clients: Mapping[str, ISocialOAuthClient],
        clock: IClock,
        refresh_skew_seconds: int = 120,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._oauth_clients = oauth_clients
        self._clock = clock
        self._refresh_skew = timedelta(seconds=refresh_skew_seconds)

    async def store(self, social_account_id: UUID, tokens: GrantedTokens) -> None:
        blob = self._cipher.encrypt(_serialize(tokens))
        now = self._clock.now()
        async with self._session_factory() as session:
            existing = await self._load_credential(session, social_account_id)
            if existing is None:
                session.add(
                    SocialCredentialRow(
                        social_account_id=social_account_id,
                        ciphertext=blob.ciphertext,
                        nonce=blob.nonce,
                        wrapped_dek=blob.wrapped_dek,
                        key_version=blob.key_version,
                        algorithm=blob.algorithm,
                        access_token_expires_at=tokens.expires_at,
                        rotated_at=now,
                    )
                )
            else:
                _apply_blob(existing, blob, expires_at=tokens.expires_at, rotated_at=now)
            await session.commit()

    async def authorize(self, social_account_id: UUID) -> AuthorizedContext:
        async with self._session_factory() as session:
            account = await self._load_account(session, social_account_id)
            if account is None or account.status != AccountStatus.CONNECTED.value:
                raise CredentialUnavailableError("account is not connected")
            credential = await self._load_credential(session, social_account_id)
            if credential is None:
                raise CredentialUnavailableError("no stored credential for account")

            tokens = _deserialize(self._cipher.decrypt(_blob_of(credential)))
            expires_at = credential.access_token_expires_at
            scopes = tuple(account.scopes)

            if self._needs_refresh(expires_at):
                refreshed = await self._refresh(account.platform, tokens)
                if refreshed is not None:
                    now = self._clock.now()
                    blob = self._cipher.encrypt(_serialize(refreshed))
                    _apply_blob(credential, blob, expires_at=refreshed.expires_at, rotated_at=now)
                    await session.commit()
                    tokens = refreshed
                    expires_at = refreshed.expires_at
                elif self._is_expired(expires_at):
                    raise CredentialUnavailableError("credential expired and cannot be refreshed")

            return AuthorizedContext(
                access_token=tokens.access_token, expires_at=expires_at, scopes=scopes
            )

    async def revoke(self, social_account_id: UUID) -> None:
        async with self._session_factory() as session:
            account = await self._load_account(session, social_account_id)
            credential = await self._load_credential(session, social_account_id)
            if credential is not None:
                await self._best_effort_provider_revoke(account, credential)
                await session.execute(
                    delete(SocialCredentialRow).where(
                        SocialCredentialRow.social_account_id == social_account_id
                    )
                )
            await session.commit()

    # ---- helpers -----------------------------------------------------------

    async def _load_account(
        self, session: AsyncSession, social_account_id: UUID
    ) -> SocialAccountRow | None:
        return (
            await session.execute(
                select(SocialAccountRow).where(SocialAccountRow.id == social_account_id)
            )
        ).scalar_one_or_none()

    async def _load_credential(
        self, session: AsyncSession, social_account_id: UUID
    ) -> SocialCredentialRow | None:
        return (
            await session.execute(
                select(SocialCredentialRow).where(
                    SocialCredentialRow.social_account_id == social_account_id
                )
            )
        ).scalar_one_or_none()

    def _needs_refresh(self, expires_at: datetime | None) -> bool:
        if expires_at is None:
            return False
        return expires_at <= self._clock.now() + self._refresh_skew

    def _is_expired(self, expires_at: datetime | None) -> bool:
        if expires_at is None:
            return False
        return expires_at <= self._clock.now()

    async def _refresh(self, platform: str, tokens: GrantedTokens) -> GrantedTokens | None:
        client = self._oauth_clients.get(platform)
        if client is None or tokens.refresh_token is None:
            return None
        try:
            refreshed = await client.refresh(refresh_token=tokens.refresh_token)
        except OAuthExchangeError:
            return None
        # Preserve the refresh token if the provider did not return a new one.
        if refreshed.refresh_token is None:
            return GrantedTokens(
                access_token=refreshed.access_token,
                refresh_token=tokens.refresh_token,
                expires_at=refreshed.expires_at,
                scopes=refreshed.scopes or tokens.scopes,
            )
        return refreshed

    async def _best_effort_provider_revoke(
        self, account: SocialAccountRow | None, credential: SocialCredentialRow
    ) -> None:
        if account is None:
            return
        client = self._oauth_clients.get(account.platform)
        if client is None:
            return
        try:
            tokens = _deserialize(self._cipher.decrypt(_blob_of(credential)))
            token = tokens.refresh_token or tokens.access_token
            await client.revoke(token=token)
        except Exception:
            return


def _serialize(tokens: GrantedTokens) -> bytes:
    return json.dumps(
        {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token}
    ).encode("utf-8")


def _deserialize(raw: bytes) -> GrantedTokens:
    data = json.loads(raw.decode("utf-8"))
    return GrantedTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=None,
        scopes=(),
    )


def _blob_of(row: SocialCredentialRow) -> EncryptedBlob:
    return EncryptedBlob(
        ciphertext=row.ciphertext,
        nonce=row.nonce,
        wrapped_dek=row.wrapped_dek,
        key_version=row.key_version,
        algorithm=row.algorithm,
    )


def _apply_blob(
    row: SocialCredentialRow,
    blob: EncryptedBlob,
    *,
    expires_at: datetime | None,
    rotated_at: datetime,
) -> None:
    row.ciphertext = blob.ciphertext
    row.nonce = blob.nonce
    row.wrapped_dek = blob.wrapped_dek
    row.key_version = blob.key_version
    row.algorithm = blob.algorithm
    row.access_token_expires_at = expires_at
    row.rotated_at = rotated_at


__all__ = ["SocialCredentialService"]
