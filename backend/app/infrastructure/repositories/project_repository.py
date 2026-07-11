"""SQLAlchemy implementation of ``IProjectRepository`` (Slice α5a).

Ships ``add`` (create), ``get_owned`` (single read), and ``list_owned``
(keyset-paginated list). All queries filter ``deleted_at IS NULL`` AND
scope to BOTH ``tenant_id`` and ``owner_user_id`` so a caller only ever
observes their own live projects (α5a D5). ``add`` maps the frozen
domain ``Project`` into an ORM row and returns a fresh domain
``Project`` reflecting the DB-populated ``id`` echo, timestamps, and
``version`` (=1).

``list_owned`` implements keyset (cursor) pagination: rows are ordered
``created_at DESC, id DESC`` (a total order — α5a D14) and, when a
cursor is supplied, filtered by the Postgres row-value comparison
``(created_at, id) < (:created_at, :id)`` so pages stay stable under
concurrent inserts.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, tuple_
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
        duration_seconds=float(row.duration_seconds) if row.duration_seconds is not None else None,
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
