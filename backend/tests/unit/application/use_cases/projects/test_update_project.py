"""Unit tests for ``UpdateProject`` (Slice α5b).

Coverage map (α5b pre-flight §5.1):

* U1 — real change: returns entity, ``version == expected + 1``, changed
  field applied, ``commit()`` once, ``project.updated`` logged.
* U2 — same-value no-op: returns unchanged entity, ``version`` unchanged,
  no CAS write, ``changed=False``, no commit.
* U3 — not visible (``get_owned`` None) → ``NotFoundError`` (404), no CAS.
* U4 — stale version (fetched.version != expected) → ``VersionConflictError``
  (412), no CAS.
* U5 — CAS race (``update_owned`` returns None after a passing fetch) →
  ``VersionConflictError`` (412).
* U6 — partial: only the sent field changes; others preserved.
* U7 — explicit-null clears a nullable field (``description``); version bumps.
* U8 — rename collision → ``ConflictError`` (409), no commit.
* U9 — scoping: the tenant/owner forwarded to ``update_owned`` are the
  caller's.

Uses the shared in-memory fakes (``..auth._fakes``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog

from app.application.use_cases.projects.update_project import (
    UpdateProject,
    UpdateProjectResult,
)
from app.core.errors import ConflictError, NotFoundError, VersionConflictError
from app.domain.projects.project import Project
from tests.unit.application.use_cases.auth._fakes import (
    FakeProjectRepository,
    FakeUnitOfWork,
)


def _make_project(
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    name: str = "Original",
    description: str | None = None,
    language: str = "en",
    style: str | None = None,
    settings: dict[str, Any] | None = None,
    version: int = 1,
) -> Project:
    now = datetime.now(UTC)
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        folder_id=None,
        current_version_id=None,
        name=name,
        description=description,
        aspect_ratio="horizontal",
        duration_seconds=None,
        language=language,
        style=style,
        settings=settings if settings is not None else {},
        created_at=now,
        updated_at=now,
        version=version,
    )


def _build_uc(
    *projects: Project,
    repo: FakeProjectRepository | None = None,
) -> tuple[UpdateProject, FakeUnitOfWork, FakeProjectRepository]:
    repo = repo or FakeProjectRepository()
    for p in projects:
        repo._rows[p.id] = p
    uow = FakeUnitOfWork(projects=repo)
    return UpdateProject(uow=uow), uow, repo


# ---- U1 — real change ------------------------------------------------


@pytest.mark.unit
async def test_u1_real_change_bumps_version_applies_field_commits_and_logs() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id, name="Before")
    uc, uow, _ = _build_uc(project)

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=project.id,
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            expected_version=1,
            changes={"name": "After"},
            ip="203.0.113.7",
        )

    assert isinstance(result, UpdateProjectResult)
    assert result.changed is True
    assert result.project.name == "After"
    assert result.project.version == 2  # expected + 1, no double-bump
    assert uow.commits == 1
    updated = [e for e in logs if e.get("event") == "project.updated"]
    assert len(updated) == 1
    ev = updated[0]
    assert ev["log_level"] == "info"
    assert ev["changed_fields"] == ["name"]
    assert ev["previous_version"] == 1
    assert ev["new_version"] == 2
    assert ev["ip"] == "203.0.113.7"
    # Project name is user content — never logged.
    assert "After" not in str(ev)


# ---- U2 — same-value no-op -------------------------------------------


@pytest.mark.unit
async def test_u2_same_value_no_op_returns_unchanged_no_write() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id, name="Same")
    uc, uow, _ = _build_uc(project)

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=project.id,
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            expected_version=1,
            changes={"name": "Same"},
        )

    assert result.changed is False
    assert result.project.version == 1  # unchanged
    assert uow.commits == 0  # no write
    rejected = [e for e in logs if e.get("event") == "project.update_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "same_value_noop"
    assert rejected[0]["log_level"] == "info"


# ---- U3 — not visible → 404 ------------------------------------------


@pytest.mark.unit
async def test_u3_not_visible_raises_not_found_no_cas() -> None:
    uc, uow, _ = _build_uc()  # empty repo
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=uuid4(),
            tenant_id=uuid4(),
            expected_version=1,
            changes={"name": "X"},
        )
    assert uow.commits == 0


# ---- U4 — stale version → 412 ----------------------------------------


@pytest.mark.unit
async def test_u4_stale_version_raises_version_conflict_no_cas() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id, version=5)
    uc, uow, _ = _build_uc(project)

    with structlog.testing.capture_logs() as logs, pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=project.id,
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            expected_version=1,  # stale (current is 5)
            changes={"name": "X"},
        )
    assert uow.commits == 0
    rejected = [e for e in logs if e.get("event") == "project.update_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "version_mismatch"
    assert rejected[0]["log_level"] == "warning"


# ---- U5 — CAS race → 412 ---------------------------------------------


class _RaceRepo(FakeProjectRepository):
    """``get_owned`` sees a live matching row, but ``update_owned`` reports
    the row vanished (concurrent bump/delete between fetch and CAS)."""

    async def update_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        expected_version: int,
        changes: Any,
    ) -> Project | None:
        return None


@pytest.mark.unit
async def test_u5_cas_race_returns_none_raises_version_conflict() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id)
    repo = _RaceRepo()
    repo._rows[project.id] = project
    uc, uow, _ = _build_uc(repo=repo)

    with pytest.raises(VersionConflictError):
        await uc.execute(
            project_id=project.id,
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            expected_version=1,
            changes={"name": "Changed"},
        )
    assert uow.commits == 0


# ---- U6 — partial (absent fields preserved) --------------------------


@pytest.mark.unit
async def test_u6_partial_leaves_absent_fields_unchanged() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        name="Keep",
        description="keep-desc",
        language="en",
        style="cinematic",
        settings={"fps": 30},
    )
    uc, _, _ = _build_uc(project)

    result = await uc.execute(
        project_id=project.id,
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        expected_version=1,
        changes={"name": "Renamed"},  # only name
    )

    assert result.project.name == "Renamed"
    assert result.project.description == "keep-desc"
    assert result.project.language == "en"
    assert result.project.style == "cinematic"
    assert result.project.settings == {"fps": 30}


# ---- U7 — explicit-null clears nullable ------------------------------


@pytest.mark.unit
async def test_u7_explicit_null_clears_nullable_and_bumps_version() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id, description="to-clear")
    uc, _, _ = _build_uc(project)

    result = await uc.execute(
        project_id=project.id,
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        expected_version=1,
        changes={"description": None},
    )

    assert result.changed is True
    assert result.project.description is None
    assert result.project.version == 2


# ---- U8 — rename collision → 409 -------------------------------------


@pytest.mark.unit
async def test_u8_rename_collision_raises_conflict_no_commit() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    a = _make_project(tenant_id=tenant_id, owner_user_id=owner_id, name="Alpha")
    b = _make_project(tenant_id=tenant_id, owner_user_id=owner_id, name="Beta")
    uc, uow, _ = _build_uc(a, b)

    with pytest.raises(ConflictError):
        await uc.execute(
            project_id=a.id,
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            expected_version=1,
            changes={"name": "Beta"},  # collides with b
        )
    assert uow.commits == 0


# ---- U9 — scoping forwarded to update_owned --------------------------


class _RecordingRepo(FakeProjectRepository):
    captured: tuple[UUID, UUID, UUID] | None = None

    async def update_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        expected_version: int,
        changes: Any,
    ) -> Project | None:
        self.captured = (project_id, tenant_id, owner_user_id)
        return await super().update_owned(
            project_id, tenant_id, owner_user_id, expected_version, changes
        )


@pytest.mark.unit
async def test_u9_scoping_forwarded_to_update_owned() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    project = _make_project(tenant_id=tenant_id, owner_user_id=owner_id, name="S")
    repo = _RecordingRepo()
    repo._rows[project.id] = project
    uc, _, _ = _build_uc(repo=repo)

    await uc.execute(
        project_id=project.id,
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        expected_version=1,
        changes={"name": "S2"},
    )

    assert repo.captured == (project.id, tenant_id, owner_id)
