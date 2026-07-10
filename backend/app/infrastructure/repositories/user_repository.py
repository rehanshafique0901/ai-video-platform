"""SQLAlchemy implementation of ``IUserRepository``.

α1 shipped ``count`` + ``exists_by_id`` (kept per the α2a review).
α2a adds ``get_by_email``, ``get_by_id``, ``add``, ``update_last_login``.
α4 adds ``update_profile`` — the version-fenced targeted mutation
underpinning ``PATCH /users/me``. Its CAS-on-``version`` shape is the
canonical example future versioned-aggregate writes (see α4 pre-flight
§10) follow.

All queries filter ``deleted_at IS NULL`` — soft-deleted rows never
surface. ``add`` maps the frozen domain ``User`` into an ORM row and
returns a fresh domain ``User`` reflecting the DB-populated
timestamps + version.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IUserRepository
from app.core.errors import ConflictError
from app.domain.identity.user import User as UserEntity
from app.infrastructure.db.models.identity import User as UserRow


class UserRepository(IUserRepository):
    """User persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- α1 keeper methods ----------------------------------------------

    async def count(self) -> int:
        stmt = select(func.count()).select_from(UserRow).where(UserRow.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def exists_by_id(self, user_id: UUID) -> bool:
        stmt = select(UserRow.id).where(UserRow.id == user_id).where(UserRow.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        return result.first() is not None

    # ---- α2a additions --------------------------------------------------

    async def get_by_email(self, email: str) -> UserEntity | None:
        stmt = (
            select(UserRow)
            .where(UserRow.email == email)
            .where(UserRow.deleted_at.is_(None))
            .order_by(UserRow.created_at.asc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def get_by_id(self, user_id: UUID) -> UserEntity | None:
        stmt = select(UserRow).where(UserRow.id == user_id).where(UserRow.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def add(self, user: UserEntity) -> UserEntity:
        row = UserRow(
            id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            password_hash=user.password_hash,
            display_name=user.display_name,
            email_verified_at=user.email_verified_at,
            last_login_at=user.last_login_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # PostgreSQL raises 23505 (unique_violation) for both
            # ``uq_users_tenant_id_email`` and (unlikely) ``pk_users``
            # collisions. Both surface here as ConflictError so the
            # router maps them to 409 CONFLICT per API_CONTRACT §1.2.
            raise ConflictError(
                "user already exists",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)

    async def update_last_login(self, user_id: UUID, at: datetime) -> None:
        stmt = (
            update(UserRow)
            .where(UserRow.id == user_id)
            .where(UserRow.deleted_at.is_(None))
            .values(last_login_at=at)
        )
        await self._session.execute(stmt)

    # ---- α4 additions --------------------------------------------------

    async def update_profile(
        self,
        user_id: UUID,
        expected_version: int,
        display_name: str,
    ) -> UserEntity | None:
        # Step 1 — version-fenced fetch. If no live row matches BOTH
        # the user_id AND the expected_version, we return None without
        # a write. Per α4 §A10 the "not found" and "version mismatch"
        # outcomes are deliberately indistinguishable here (both
        # surface as 412 VERSION_CONFLICT at the API boundary).
        fetch = (
            select(UserRow)
            .where(UserRow.id == user_id)
            .where(UserRow.version == expected_version)
            .where(UserRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(fetch)).scalar_one_or_none()
        if row is None:
            return None

        # Step 2 — same-value no-op (α4 §D8 + §D6a). The row exists,
        # the version matches, but ``display_name`` already equals the
        # target value. Return the unchanged entity — no UPDATE, no
        # version bump, no ``updated_at`` bump, no dirty replication
        # log. The wire response looks identical to a real change;
        # only the server-side structured log distinguishes the two.
        if row.display_name == display_name:
            return _row_to_entity(row)

        # Step 3 — real change. The CAS filter (``version = :expected``)
        # in the WHERE clause is what guarantees atomicity: if a
        # concurrent writer bumped the version between step 1 and
        # step 3, ``RETURNING`` yields zero rows and we return None.
        # The row lock granted by UPDATE plus MVCC snapshot
        # re-evaluation is sufficient — no explicit ``FOR UPDATE``
        # needed for this low-contention aggregate. If contention
        # ever becomes real (admin edits, batch operations), add
        # ``.with_for_update()`` to the step-1 fetch.
        #
        # ``RETURNING(UserRow)`` (Postgres) delivers the post-UPDATE
        # row in the same round-trip, so we observe the incremented
        # ``version`` and server-side ``updated_at`` (from
        # ``func.now()``) without a follow-up SELECT.
        upd = (
            update(UserRow)
            .where(UserRow.id == user_id)
            .where(UserRow.version == expected_version)
            .where(UserRow.deleted_at.is_(None))
            .values(
                display_name=display_name,
                version=UserRow.version + 1,
                updated_at=func.now(),
            )
            .returning(UserRow)
        )
        updated_row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(updated_row) if updated_row is not None else None


def _row_to_entity(row: UserRow) -> UserEntity:
    return UserEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        email=row.email,
        password_hash=row.password_hash,
        display_name=row.display_name,
        email_verified_at=row.email_verified_at,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
