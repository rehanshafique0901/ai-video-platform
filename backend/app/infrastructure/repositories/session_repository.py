"""SQLAlchemy implementation of ``ISessionRepository``.

α2a shipped ``add``. α2b extends the adapter with:

* ``get_by_hash`` — SHA-256 lookup driving the refresh flow. Returns
  revoked rows too; the ``RefreshSession`` use case is what interprets
  ``revoked_at != NULL`` as the reuse signal.
* ``revoke`` — atomic compare-and-swap update. ``UPDATE ... WHERE id
  = :sid AND revoked_at IS NULL`` returns ``rowcount == 0`` if the row
  is already revoked or missing, which the caller treats as an
  idempotent no-op (matches the port contract).
* ``list_family`` — enumerate every row for a rotation family. Used by
  the reuse-detection path in ``RefreshSession``.

α3 extends the adapter with:

* ``get_by_id`` — sid-driven lookup for ``get_current_user`` (the
  authenticated-request dep). Access tokens carry ``sid`` directly,
  so the request-authentication path has no token hash to look up by.
  Returns revoked rows too, mirroring ``get_by_hash``'s caller-decides
  contract.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
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
        return _row_to_entity(row)

    async def get_by_hash(self, token_hash: str) -> SessionEntity | None:
        stmt = select(SessionRow).where(SessionRow.token_hash == token_hash)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def get_by_id(self, session_id: UUID) -> SessionEntity | None:
        # α3: sid-driven lookup for ``get_current_user``. Returns revoked
        # rows too so the dep can distinguish ``session_revoked`` from
        # ``session_expired`` in its structured log.
        stmt = select(SessionRow).where(SessionRow.id == session_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def revoke(self, session_id: UUID, at: datetime) -> bool:
        stmt = (
            update(SessionRow)
            .where(SessionRow.id == session_id)
            .where(SessionRow.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        result = await self._session.execute(stmt)
        # rowcount == 1 for a successful CAS; 0 if the row was already
        # revoked or does not exist. Both non-happy cases are safe
        # no-ops per the port contract. mypy: ``Result`` is generic and
        # its stub omits ``rowcount``; at runtime an UPDATE returns a
        # ``CursorResult`` which always carries it.
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def list_family(self, family_id: UUID) -> list[SessionEntity]:
        stmt = select(SessionRow).where(SessionRow.family_id == family_id)
        result = await self._session.execute(stmt)
        return [_row_to_entity(row) for row in result.scalars()]


def _row_to_entity(row: SessionRow) -> SessionEntity:
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
