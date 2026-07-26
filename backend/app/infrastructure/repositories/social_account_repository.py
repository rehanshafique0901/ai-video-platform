"""SQLAlchemy implementation of ``ISocialAccountRepository`` (α8.6a).

Persists the publishing context's non-secret account profile + lifecycle status. The OAuth
secret is owned by the credential service (``social_credentials``); this adapter never
touches token material. Owner-scoped by ``tenant_id`` + ``user_id`` with the uniform
anti-enumeration posture (foreign/missing id → ``None`` → 404 upstream), mirroring
``MediaRepository`` / ``NotificationRepository``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import ISocialAccountRepository
from app.domain.publishing.social_account import AccountStatus, SocialAccount as SocialAccountEntity
from app.infrastructure.db.models.publishing import SocialAccount as SocialAccountRow


class SocialAccountRepository(ISocialAccountRepository):
    """Social-account persistence adapter (owner-scoped, publishing context)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_connected(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        platform: str,
        external_account_id: str,
        display_name: str | None,
        scopes: tuple[str, ...],
    ) -> SocialAccountEntity:
        row = (
            await self._session.execute(
                select(SocialAccountRow)
                .where(SocialAccountRow.user_id == user_id)
                .where(SocialAccountRow.platform == platform)
                .where(SocialAccountRow.external_account_id == external_account_id)
            )
        ).scalar_one_or_none()

        if row is None:
            row = SocialAccountRow(
                tenant_id=tenant_id,
                user_id=user_id,
                platform=platform,
                external_account_id=external_account_id,
                display_name=display_name,
                status=AccountStatus.CONNECTED.value,
                scopes=list(scopes),
                connected_at=func.now(),
            )
            self._session.add(row)
        else:
            row.tenant_id = tenant_id
            row.display_name = display_name
            row.status = AccountStatus.CONNECTED.value
            row.scopes = list(scopes)
            row.connected_at = func.now()
            row.revoked_at = None
            row.updated_at = func.now()

        await self._session.flush()
        await self._session.refresh(row)
        return _row_to_entity(row)

    async def get_owned(
        self, *, tenant_id: UUID, user_id: UUID, social_account_id: UUID
    ) -> SocialAccountEntity | None:
        row = await self._get_owned_row(tenant_id, user_id, social_account_id)
        return _row_to_entity(row) if row is not None else None

    async def list_for_owner(self, *, tenant_id: UUID, user_id: UUID) -> list[SocialAccountEntity]:
        rows = (
            (
                await self._session.execute(
                    select(SocialAccountRow)
                    .where(SocialAccountRow.tenant_id == tenant_id)
                    .where(SocialAccountRow.user_id == user_id)
                    .order_by(SocialAccountRow.created_at.desc(), SocialAccountRow.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_entity(row) for row in rows]

    async def mark_revoked(
        self, *, tenant_id: UUID, user_id: UUID, social_account_id: UUID
    ) -> SocialAccountEntity | None:
        row = await self._get_owned_row(tenant_id, user_id, social_account_id)
        if row is None:
            return None
        if row.status != AccountStatus.REVOKED.value:
            row.status = AccountStatus.REVOKED.value
            row.revoked_at = func.now()
            row.updated_at = func.now()
            await self._session.flush()
            await self._session.refresh(row)
        return _row_to_entity(row)

    async def _get_owned_row(
        self, tenant_id: UUID, user_id: UUID, social_account_id: UUID
    ) -> SocialAccountRow | None:
        return (
            await self._session.execute(
                select(SocialAccountRow)
                .where(SocialAccountRow.id == social_account_id)
                .where(SocialAccountRow.tenant_id == tenant_id)
                .where(SocialAccountRow.user_id == user_id)
            )
        ).scalar_one_or_none()


def _row_to_entity(row: SocialAccountRow) -> SocialAccountEntity:
    return SocialAccountEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        platform=row.platform,
        external_account_id=row.external_account_id,
        display_name=row.display_name,
        status=AccountStatus(row.status),
        scopes=tuple(row.scopes),
        connected_at=row.connected_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = ["SocialAccountRepository"]
