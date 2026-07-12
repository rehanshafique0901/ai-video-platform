"""Unit tests for ``ListProjectVersions`` + ``GetProjectVersion`` (Slice α5d.1).

Coverage map (α5d pre-flight §7 / §8):

* U6 — list returns version metadata newest-first (``version_number`` DESC).
* U7 — list on an unowned project → ``NotFoundError``.
* U8 — list on an owned project with no versions → ``[]``.
* U9 — get returns the full version WITH its snapshot, addressed by UUID.
* U10 — get on an unowned project → ``NotFoundError`` (project gate first).
* U11 — get of an unknown version id under an owned project → ``NotFoundError``.
* U12 — get of a version belonging to ANOTHER project → ``NotFoundError``
  (cross-project isolation).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.versions.create_version import CreateProjectVersion
from app.application.use_cases.versions.get_version import GetProjectVersion
from app.application.use_cases.versions.list_versions import ListProjectVersions
from app.core.errors import NotFoundError
from app.domain.versions.project_version import ProjectVersionSummary
from tests.unit.application.use_cases.versions._helpers import build_env


async def _capture(env, n: int) -> list:  # type: ignore[no-untyped-def]
    uc = CreateProjectVersion(uow=env.uow)
    out = []
    for _ in range(n):
        result = await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
        out.append(result.version)
    return out


@pytest.mark.unit
async def test_u6_list_newest_first() -> None:
    env = build_env()
    await _capture(env, 3)
    uc = ListProjectVersions(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    versions = result.versions

    assert all(isinstance(v, ProjectVersionSummary) for v in versions)
    assert [v.version_number for v in versions] == [3, 2, 1]
    # The newest capture is the project's current version (linear α5d.1 chain).
    assert result.current_version_id == versions[0].id


@pytest.mark.unit
async def test_u7_list_unowned_project_raises_not_found() -> None:
    env = build_env()
    uc = ListProjectVersions(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u8_list_no_versions_returns_empty() -> None:
    env = build_env()
    uc = ListProjectVersions(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert result.versions == []
    assert result.current_version_id is None


@pytest.mark.unit
async def test_u9_get_returns_full_snapshot() -> None:
    env = build_env()
    (created,) = await _capture(env, 1)
    uc = GetProjectVersion(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        version_id=created.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )
    version = result.version

    assert version.id == created.id
    assert version.snapshot["schema_version"] == 1
    assert "project" in version.snapshot
    # The single captured version is current.
    assert result.current_version_id == created.id


@pytest.mark.unit
async def test_u10_get_unowned_project_raises_not_found() -> None:
    env = build_env()
    (created,) = await _capture(env, 1)
    uc = GetProjectVersion(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            version_id=created.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u11_get_unknown_version_raises_not_found() -> None:
    env = build_env()
    await _capture(env, 1)
    uc = GetProjectVersion(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            version_id=uuid4(),  # unknown version
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u12_get_version_of_another_project_raises_not_found() -> None:
    # Two owned projects; a version captured under project A must not be
    # readable via project B's path (cross-project isolation).
    env = build_env()
    (created_a,) = await _capture(env, 1)

    # Seed a second owned project for the same caller.
    from tests.unit.application.use_cases.versions._helpers import make_project

    project_b = make_project(tenant_id=env.tenant_id, owner_user_id=env.owner_user_id)
    env.projects._rows[project_b.id] = project_b

    uc = GetProjectVersion(uow=env.uow)
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=project_b.id,
            version_id=created_a.id,  # belongs to project A
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )
