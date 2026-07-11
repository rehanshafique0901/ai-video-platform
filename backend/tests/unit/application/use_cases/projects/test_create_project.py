"""Unit tests for ``CreateProject`` (Slice α5a).

Coverage map (α5a pre-flight §8):

* U1 — happy path: returns the persisted project with ``version == 1``,
  writes to the repo, commits exactly once.
* U2 — ownership + tenancy are taken from the ``execute`` args (the
  authenticated caller), never invented — the persisted row carries
  them verbatim (§D5).
* U3 — duplicate live name for the owner raises ``ConflictError``, does
  NOT commit, and leaves the repo unchanged.
* U4 — ``settings`` defaults to ``{}`` when omitted; supplied settings
  are stored verbatim.
* U5 — happy path emits ``project.created`` (INFO) with the field set
  from §8; the project ``name`` (user content) is never in the log.
* U6 — duplicate emits ``project.create_rejected`` (WARN) with
  ``reason=duplicate_name``; no ``project.created`` event.

Uses the shared in-memory fakes (``..auth._fakes``) — the canonical
fake surface for the whole application layer (see α4 note in
``users/test_update_profile.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.projects.create_project import (
    CreateProject,
    CreateProjectResult,
)
from app.core.errors import ConflictError
from app.domain.projects.project import Project
from tests.unit.application.use_cases.auth._fakes import (
    FakeProjectRepository,
    FakeUnitOfWork,
)


def _dt() -> datetime:
    return datetime.now(UTC)


def _build_uc(
    projects: FakeProjectRepository | None = None,
) -> tuple[CreateProject, FakeUnitOfWork, FakeProjectRepository]:
    repo = projects or FakeProjectRepository()
    uow = FakeUnitOfWork(projects=repo)
    return CreateProject(uow=uow), uow, repo


# ---- U1 — happy path -------------------------------------------------


@pytest.mark.unit
async def test_u1_happy_path_creates_project_version_one_and_commits() -> None:
    uc, uow, repo = _build_uc()
    owner_id = uuid4()
    tenant_id = uuid4()

    result = await uc.execute(
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        name="My First Video",
        aspect_ratio="horizontal",
    )

    assert isinstance(result, CreateProjectResult)
    assert result.project.name == "My First Video"
    assert result.project.aspect_ratio == "horizontal"
    assert result.project.version == 1
    assert result.project.language == "en"  # default
    # Persisted to the repo (not merely returned by value).
    assert repo._rows[result.project.id] is result.project
    assert uow.commits == 1
    assert uow.rollbacks == 0


# ---- U2 — ownership + tenancy from the caller ------------------------


@pytest.mark.unit
async def test_u2_ownership_and_tenancy_taken_from_caller() -> None:
    uc, _, repo = _build_uc()
    owner_id = uuid4()
    tenant_id = uuid4()

    result = await uc.execute(
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        name="Scoped",
        aspect_ratio="vertical",
    )

    assert result.project.owner_user_id == owner_id
    assert result.project.tenant_id == tenant_id
    persisted = repo._rows[result.project.id]
    assert persisted.owner_user_id == owner_id
    assert persisted.tenant_id == tenant_id
    # α5a leaves these unset (designed-not-shipped).
    assert persisted.folder_id is None
    assert persisted.current_version_id is None
    assert persisted.duration_seconds is None


# ---- U3 — duplicate name → ConflictError, no commit ------------------


@pytest.mark.unit
async def test_u3_duplicate_name_raises_conflict_and_does_not_commit() -> None:
    owner_id = uuid4()
    tenant_id = uuid4()
    existing = Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        folder_id=None,
        current_version_id=None,
        name="Dup",
        description=None,
        aspect_ratio="square",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=_dt(),
        updated_at=_dt(),
        version=1,
    )
    repo = FakeProjectRepository(_rows={existing.id: existing})
    uc, uow, _ = _build_uc(projects=repo)

    with pytest.raises(ConflictError):
        await uc.execute(
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            name="Dup",
            aspect_ratio="horizontal",
        )

    assert uow.commits == 0
    assert len(repo._rows) == 1  # nothing added


# ---- U4 — settings default + verbatim --------------------------------


@pytest.mark.unit
async def test_u4_settings_default_empty_and_verbatim() -> None:
    uc, _, _ = _build_uc()
    default_result = await uc.execute(
        owner_user_id=uuid4(),
        tenant_id=uuid4(),
        name="Defaults",
        aspect_ratio="horizontal",
    )
    assert default_result.project.settings == {}

    payload = {"fps": 30, "captions": True}
    supplied_result = await uc.execute(
        owner_user_id=uuid4(),
        tenant_id=uuid4(),
        name="Supplied",
        aspect_ratio="horizontal",
        settings=payload,
    )
    assert supplied_result.project.settings == payload


# ---- U5 — happy-path log ---------------------------------------------


@pytest.mark.unit
async def test_u5_happy_path_emits_project_created_log() -> None:
    uc, _, _ = _build_uc()
    owner_id = uuid4()
    tenant_id = uuid4()

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            name="Logged Project",
            aspect_ratio="vertical",
            ip="203.0.113.7",
        )

    created = [e for e in logs if e.get("event") == "project.created"]
    assert len(created) == 1
    ev = created[0]
    assert ev["log_level"] == "info"
    assert ev["project_id"] == str(result.project.id)
    assert ev["owner_user_id"] == str(owner_id)
    assert ev["tenant_id"] == str(tenant_id)
    assert ev["aspect_ratio"] == "vertical"
    assert ev["ip"] == "203.0.113.7"
    # Project name is user content — never logged.
    assert "Logged Project" not in str(ev)


# ---- U6 — duplicate log ----------------------------------------------


@pytest.mark.unit
async def test_u6_duplicate_emits_rejected_warn_log() -> None:
    owner_id = uuid4()
    tenant_id = uuid4()
    existing = Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        folder_id=None,
        current_version_id=None,
        name="Dup",
        description=None,
        aspect_ratio="square",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=_dt(),
        updated_at=_dt(),
        version=1,
    )
    repo = FakeProjectRepository(_rows={existing.id: existing})
    uc, _, _ = _build_uc(projects=repo)

    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(ConflictError),
    ):
        await uc.execute(
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            name="Dup",
            aspect_ratio="horizontal",
            ip="203.0.113.7",
        )

    rejected = [e for e in logs if e.get("event") == "project.create_rejected"]
    assert len(rejected) == 1
    ev = rejected[0]
    assert ev["log_level"] == "warning"
    assert ev["reason"] == "duplicate_name"
    assert ev["owner_user_id"] == str(owner_id)
    assert ev["ip"] == "203.0.113.7"
    assert not any(e.get("event") == "project.created" for e in logs)
