"""``CreateProject`` use case (Slice α5a).

Contract (API_CONTRACT §3.2):

    POST /api/v1/projects
      body:  { name, aspect_ratio, description?, language?, style?, settings? }
      → 201  { data: ProjectPublic (version=1), meta }
      → 409  { error: { code: CONFLICT, ... } }          (duplicate live name)
      → 422  { error: { code: VALIDATION_FAILED, ... } }  (via DTO)
      → 401  { error: { code: UNAUTHENTICATED, ... } }    (via CurrentUserDep)

Ownership + tenancy are taken from the authenticated caller
(``owner_user_id`` / ``tenant_id`` resolved by ``CurrentUserDep``),
never from the request body — a client cannot create a project under
another owner or tenant (see ``docs/domain/PROJECT_AGGREGATE.md`` §2).

The new row's ``id`` and ``version`` (=1) are server-assigned; the
version fence that guards *mutations* (α5b ``PATCH``) does not apply to
an insert (α5a pre-flight D11). The ``created_at`` / ``updated_at`` /
``version`` values set on the entity below are placeholders discarded by
the repository — the DB server defaults populate them and the returned
entity carries the authoritative values (same pattern as
``RegisterUser`` → ``UserRepository.add``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError
from app.domain.projects.project import Project

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CreateProjectResult:
    """Outcome of a successful ``CreateProject.execute`` — the persisted project."""

    project: Project


class CreateProject:
    """Create a project owned by the authenticated caller."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        owner_user_id: UUID,
        tenant_id: UUID,
        name: str,
        aspect_ratio: str,
        description: str | None = None,
        language: str = "en",
        style: str | None = None,
        settings: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> CreateProjectResult:
        now = datetime.now(UTC)  # placeholder; DB defaults are authoritative
        candidate = Project(
            id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            folder_id=None,
            current_version_id=None,
            name=name,
            description=description,
            aspect_ratio=aspect_ratio,
            duration_seconds=None,
            language=language,
            style=style,
            settings=settings if settings is not None else {},
            created_at=now,
            updated_at=now,
            version=1,
        )

        async with self._uow:
            try:
                created = await self._uow.projects.add(candidate)
            except ConflictError:
                # Duplicate live name for this owner. Surface as-is
                # (handler → 409 CONFLICT); log at WARN for visibility.
                # ``name`` is user content, not logged.
                _LOGGER.warning(
                    "project.create_rejected",
                    reason="duplicate_name",
                    owner_user_id=str(owner_user_id),
                    tenant_id=str(tenant_id),
                    ip=ip,
                )
                raise
            await self._uow.commit()

        _LOGGER.info(
            "project.created",
            project_id=str(created.id),
            owner_user_id=str(created.owner_user_id),
            tenant_id=str(created.tenant_id),
            aspect_ratio=created.aspect_ratio,
            ip=ip,
        )
        return CreateProjectResult(project=created)
