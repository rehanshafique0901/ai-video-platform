"""Unit tests for ``GetProject`` (Slice α5a).

Coverage map (α5a pre-flight §8):

* U1 — happy path: returns the owner's project.
* U2 — absent project raises ``NotFoundError``.
* U3 — cross-owner project raises ``NotFoundError`` (owner-scoped —
  §D5, anti-enumeration: "not yours" is indistinguishable from
  "does not exist").
* U4 — cross-tenant project raises ``NotFoundError`` (tenant-scoped).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.projects.get_project import GetProject
from app.core.errors import NotFoundError
from app.domain.projects.project import Project
from tests.unit.application.use_cases.auth._fakes import (
    FakeProjectRepository,
    FakeUnitOfWork,
)


def _make_project(*, tenant_id: UUID, owner_user_id: UUID) -> Project:
    now = datetime.now(UTC)
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        folder_id=None,
        current_version_id=None,
        name="P",
        description=None,
        aspect_ratio="horizontal",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=now,
        updated_at=now,
        version=1,
    )


def _build_uc(project: Project | None) -> GetProject:
    repo = FakeProjectRepository()
    if project is not None:
        repo._rows[project.id] = project
    return GetProject(uow=FakeUnitOfWork(projects=repo))


@pytest.mark.unit
async def test_u1_happy_path_returns_owned_project() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id)
    uc = _build_uc(project)

    got = await uc.execute(project_id=project.id, owner_user_id=owner_id, tenant_id=tenant_id)
    assert got.id == project.id


@pytest.mark.unit
async def test_u2_absent_project_raises_not_found() -> None:
    uc = _build_uc(None)
    with pytest.raises(NotFoundError):
        await uc.execute(project_id=uuid4(), owner_user_id=uuid4(), tenant_id=uuid4())


@pytest.mark.unit
async def test_u3_cross_owner_raises_not_found() -> None:
    tenant_id = uuid4()
    owner_id = uuid4()
    other_owner = uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=other_owner)
    uc = _build_uc(project)

    with pytest.raises(NotFoundError):
        await uc.execute(project_id=project.id, owner_user_id=owner_id, tenant_id=tenant_id)


@pytest.mark.unit
async def test_u4_cross_tenant_raises_not_found() -> None:
    owner_id = uuid4()
    project = _make_project(tenant_id=uuid4(), owner_user_id=owner_id)
    uc = _build_uc(project)

    with pytest.raises(NotFoundError):
        await uc.execute(project_id=project.id, owner_user_id=owner_id, tenant_id=uuid4())
