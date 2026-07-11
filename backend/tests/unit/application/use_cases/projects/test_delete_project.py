"""Unit tests for ``DeleteProject`` (Slice α5b).

Coverage map (α5b pre-flight §5.1):

* U10 — happy path: ``soft_delete_owned`` True → commit once,
  ``project.deleted`` logged.
* U11 — not visible: ``soft_delete_owned`` False → ``NotFoundError``
  (404), no commit, ``project.delete_rejected`` (WARN) logged.
* U12 — idempotent: a second delete on the already-deleted project →
  ``NotFoundError`` (the fake drops the row on first delete).
* U13 — scoping: the tenant/owner forwarded to ``soft_delete_owned`` are
  the caller's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import structlog

from app.application.use_cases.projects.delete_project import DeleteProject
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
        name="ToDelete",
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


def _build_uc(
    *projects: Project,
    repo: FakeProjectRepository | None = None,
) -> tuple[DeleteProject, FakeUnitOfWork, FakeProjectRepository]:
    repo = repo or FakeProjectRepository()
    for p in projects:
        repo._rows[p.id] = p
    uow = FakeUnitOfWork(projects=repo)
    return DeleteProject(uow=uow), uow, repo


# ---- U10 — happy path ------------------------------------------------


@pytest.mark.unit
async def test_u10_happy_path_soft_deletes_commits_and_logs() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id)
    uc, uow, repo = _build_uc(project)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            project_id=project.id,
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            ip="203.0.113.7",
        )

    assert project.id not in repo._rows  # gone from the live view
    assert uow.commits == 1
    deleted = [e for e in logs if e.get("event") == "project.deleted"]
    assert len(deleted) == 1
    ev = deleted[0]
    assert ev["log_level"] == "info"
    assert ev["project_id"] == str(project.id)
    assert ev["owner_user_id"] == str(owner_id)
    assert ev["tenant_id"] == str(tenant_id)
    assert ev["ip"] == "203.0.113.7"


# ---- U11 — not visible → 404 -----------------------------------------


@pytest.mark.unit
async def test_u11_not_visible_raises_not_found_no_commit() -> None:
    uc, uow, _ = _build_uc()  # empty repo
    with structlog.testing.capture_logs() as logs, pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=uuid4(),
            tenant_id=uuid4(),
        )
    assert uow.commits == 0
    rejected = [e for e in logs if e.get("event") == "project.delete_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "not_visible"
    assert rejected[0]["log_level"] == "warning"


# ---- U12 — idempotent-by-404 -----------------------------------------


@pytest.mark.unit
async def test_u12_second_delete_raises_not_found() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id)
    uc, _, _ = _build_uc(project)

    # First delete succeeds.
    await uc.execute(project_id=project.id, owner_user_id=owner_id, tenant_id=tenant_id)
    # Second delete on the now-gone project → 404.
    with pytest.raises(NotFoundError):
        await uc.execute(project_id=project.id, owner_user_id=owner_id, tenant_id=tenant_id)


# ---- U13 — scoping forwarded to soft_delete_owned --------------------


class _RecordingRepo(FakeProjectRepository):
    captured: tuple[UUID, UUID, UUID] | None = None

    async def soft_delete_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> bool:
        self.captured = (project_id, tenant_id, owner_user_id)
        return await super().soft_delete_owned(project_id, tenant_id, owner_user_id)


@pytest.mark.unit
async def test_u13_scoping_forwarded_to_soft_delete_owned() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id)
    repo = _RecordingRepo()
    repo._rows[project.id] = project
    uc, _, _ = _build_uc(repo=repo)

    await uc.execute(project_id=project.id, owner_user_id=owner_id, tenant_id=tenant_id)

    assert repo.captured == (project.id, tenant_id, owner_id)
