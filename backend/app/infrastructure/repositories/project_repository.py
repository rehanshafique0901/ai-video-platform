"""SQLAlchemy implementation of ``IProjectRepository`` (Slices α5a + α5b).

α5a shipped ``add`` (create), ``get_owned`` (single read), and
``list_owned`` (keyset-paginated list). α5b adds the two write paths:
``update_owned`` (version-fenced CAS) and ``soft_delete_owned``
(owner-scoped soft delete). All queries filter ``deleted_at IS NULL``
AND scope to BOTH ``tenant_id`` and ``owner_user_id`` so a caller only
ever observes / mutates their own live projects (α5a D5). ``add`` maps
the frozen domain ``Project`` into an ORM row and returns a fresh domain
``Project`` reflecting the DB-populated ``id`` echo, timestamps, and
``version`` (=1).

``list_owned`` implements keyset (cursor) pagination: rows are ordered
``created_at DESC, id DESC`` (a total order — α5a D14) and, when a
cursor is supplied, filtered by the Postgres row-value comparison
``(created_at, id) < (:created_at, :id)`` so pages stay stable under
concurrent inserts. Migration ``0008`` adds a composite partial index
(``ix_projects_owner_created_id``) matching this scan (α5b M3/D10).

``update_owned`` is the α4 optimistic-concurrency CAS (``UPDATE ...
WHERE version = :expected``) applied to a path-addressed resource;
``soft_delete_owned`` sets ``deleted_at`` and reports whether a live
owned row was actually marked (idempotent-by-404 at the use-case layer).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IProjectRepository
from app.core.errors import ConflictError
from app.domain.projects.project import Project as ProjectEntity
from app.infrastructure.db.models.projects import Project as ProjectRow


class ProjectRepository(IProjectRepository):
    """Project persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, project: ProjectEntity) -> ProjectEntity:
        row = ProjectRow(
            id=project.id,
            tenant_id=project.tenant_id,
            owner_user_id=project.owner_user_id,
            folder_id=project.folder_id,
            name=project.name,
            description=project.description,
            aspect_ratio=project.aspect_ratio,
            duration_seconds=project.duration_seconds,
            language=project.language,
            style=project.style,
            settings=project.settings,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # PostgreSQL raises 23505 (unique_violation) for the live-row
            # partial-unique index ``uq_projects_tenant_id_owner_user_id_name``
            # (and, unlikely, ``pk_projects``). Both surface here as
            # ConflictError so the handler maps them to 409 CONFLICT per
            # API_CONTRACT §1.2.
            raise ConflictError(
                "project already exists",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)

    async def get_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> ProjectEntity | None:
        stmt = (
            select(ProjectRow)
            .where(ProjectRow.id == project_id)
            .where(ProjectRow.tenant_id == tenant_id)
            .where(ProjectRow.owner_user_id == owner_user_id)
            .where(ProjectRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def list_owned(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[ProjectEntity]:
        stmt = (
            select(ProjectRow)
            .where(ProjectRow.tenant_id == tenant_id)
            .where(ProjectRow.owner_user_id == owner_user_id)
            .where(ProjectRow.deleted_at.is_(None))
        )
        if after is not None:
            after_created_at, after_id = after
            # Row-value comparison — the keyset predicate. With the
            # DESC ordering below, "strictly after the cursor" means a
            # smaller ``(created_at, id)`` tuple.
            stmt = stmt.where(
                tuple_(ProjectRow.created_at, ProjectRow.id) < (after_created_at, after_id)
            )
        stmt = stmt.order_by(ProjectRow.created_at.desc(), ProjectRow.id.desc()).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_entity(row) for row in rows]

    async def update_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> ProjectEntity | None:
        # Version-fenced CAS — mirrors ``UserRepository.update_profile``.
        # The WHERE clause is the atomicity guarantee: only a live, owned,
        # correctly-versioned row is updated. If a concurrent writer bumped
        # ``version`` OR soft-deleted the row after the use case's
        # ``get_owned`` (the α5b D3 fetch-then-fence), ``RETURNING`` yields
        # zero rows and we return None → the use case maps that to 412.
        #
        # ``version = ProjectRow.version + 1`` is hand-set even though the
        # ``tg_projects_biu_version_bump`` trigger (baseline 0001) exists,
        # because that trigger is GUARDED: it only bumps when
        # ``NEW.version = OLD.version``. Hand-setting ``+1`` makes
        # ``NEW.version != OLD.version``, so the trigger no-ops and the net
        # increment is exactly +1 (verified by R8). ``updated_at`` is set
        # here too and also owned by ``tg_projects_biu_touch_updated_at`` —
        # both resolve to ``now()``, so it is harmless and keeps the CAS
        # shape identical to α4.
        assert changes, "update_owned requires at least one changed column"
        upd = (
            update(ProjectRow)
            .where(ProjectRow.id == project_id)
            .where(ProjectRow.tenant_id == tenant_id)
            .where(ProjectRow.owner_user_id == owner_user_id)
            .where(ProjectRow.version == expected_version)
            .where(ProjectRow.deleted_at.is_(None))
            .values(**changes, version=ProjectRow.version + 1, updated_at=func.now())
            .returning(ProjectRow)
        )
        try:
            updated_row = (await self._session.execute(upd)).scalar_one_or_none()
        except IntegrityError as e:
            # A rename to a ``name`` already held by another live project of
            # the same (tenant, owner) violates the partial-unique index
            # ``uq_projects_tenant_id_owner_user_id_name`` → 409, identical
            # to ``add`` (α5b D9). No pre-check SELECT — the DB constraint
            # is the arbiter (TOCTOU-free).
            raise ConflictError(
                "project already exists",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        return _row_to_entity(updated_row) if updated_row is not None else None

    async def soft_delete_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> bool:
        # Owner+tenant-scoped soft delete. ``deleted_at IS NULL`` in the
        # WHERE makes a repeat delete match no row → returns False → the
        # use case maps that to 404 (idempotent-by-404, α5b D6). No version
        # fence (α5b D8): a soft delete is not a partial overwrite, so
        # optimistic concurrency adds friction without safety. ``RETURNING
        # id`` lets us distinguish "row marked" from "nothing matched"
        # without a follow-up SELECT.
        stmt = (
            update(ProjectRow)
            .where(ProjectRow.id == project_id)
            .where(ProjectRow.tenant_id == tenant_id)
            .where(ProjectRow.owner_user_id == owner_user_id)
            .where(ProjectRow.deleted_at.is_(None))
            .values(deleted_at=func.now())
            .returning(ProjectRow.id)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        return marked is not None

    async def touch_version(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> int | None:
        # Aggregate OCC Rule (α5d.2 Q1) — advance the project-aggregate OCC
        # token after a real child (scene) mutation. Same hand-set ``+1`` over
        # the guarded ``tg_projects_biu_version_bump`` trigger as
        # ``update_owned`` (net exactly +1), scoped owner+tenant+live. No
        # version fence: the child mutation carried its own fence; this is the
        # aggregate roll-up, so it advances whatever the current value is.
        # ``RETURNING version`` reports the new token (or None if a concurrent
        # soft-delete removed the row — the outer transaction handles it).
        upd = (
            update(ProjectRow)
            .where(ProjectRow.id == project_id)
            .where(ProjectRow.tenant_id == tenant_id)
            .where(ProjectRow.owner_user_id == owner_user_id)
            .where(ProjectRow.deleted_at.is_(None))
            .values(version=ProjectRow.version + 1, updated_at=func.now())
            .returning(ProjectRow.version)
        )
        return (await self._session.execute(upd)).scalar_one_or_none()


def _row_to_entity(row: ProjectRow) -> ProjectEntity:
    return ProjectEntity(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        folder_id=row.folder_id,
        current_version_id=row.current_version_id,
        name=row.name,
        description=row.description,
        aspect_ratio=row.aspect_ratio,
        # ``duration_seconds`` is ``Numeric(10,3)`` → psycopg returns a
        # ``Decimal``; the domain entity models it as ``float | None``.
        # α5a never sets it (always None on create) but a row fetched
        # from a later slice could carry a value, so convert defensively.
        duration_seconds=(
            float(row.duration_seconds) if row.duration_seconds is not None else None
        ),
        language=row.language,
        style=row.style,
        settings=row.settings,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``user_repository.py``. Duplicated rather than
    shared because the two repositories are the only consumers and a
    one-line ``getattr`` chain is cheaper to read inline than a shared
    infra-utility import; extract to a shared module if a third
    repository needs it.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
