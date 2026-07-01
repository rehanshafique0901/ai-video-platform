"""SQLAlchemy implementation of ``ISessionRepository`` (α2a: insert-only).

α2b extends this class with ``get_by_hash`` (lookup for refresh),
``revoke`` (single-row revoke for logout + used-refresh-token), and
``list_family`` (used by reuse detection to revoke a whole rotation
family). Kept intentionally narrow now.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import ISessionRepository
from app.core.errors import ConflictError
from app.domain.identity.session import Session as SessionEntity
from app.infrastructure.db.models.identity import Session as SessionRow


class SessionRepository(ISessionRepository):
    """Refresh-session persistence adapter."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, session: SessionEntity) -> SessionEntity:
        row = SessionRow(
            id=session.id,
            user_id=session.user_id,
            family_id=session.family_id,
            token_hash=session.token_hash,
            ip=session.ip,
            user_agent=session.user_agent,
            issued_at=session.issued_at,
            last_used_at=session.last_used_at,
            expires_at=session.expires_at,
            revoked_at=session.revoked_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # ``uq_sessions_token_hash`` collision is essentially
            # impossible in practice (SHA-256 over a signed JWT that
            # contains uuid4 + timestamps + secret-based signature),
            # so the only realistic path here is a duplicate id which
            # itself is a caller bug. Surface as CONFLICT anyway.
            raise ConflictError(
                "session already exists",
                details={"session_id": str(session.id)},
            ) from e
        await self._session.refresh(row)
        return SessionEntity(
            id=row.id,
            user_id=row.user_id,
            family_id=row.family_id,
            token_hash=row.token_hash,
            ip=row.ip,
            user_agent=row.user_agent,
            issued_at=row.issued_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )
