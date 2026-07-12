"""Shared scaffolding for the α5d.1 Project Version use-case unit tests.

Every version use case runs the **project ownership gate** (mirrors α5c): it
first calls ``projects.get_owned`` and 404s if the caller does not own a live
project, then reaches the append-only version ledger. So every test needs a
seeded, owned :class:`Project`. ``build_env`` wires a ``FakeUnitOfWork`` whose
version fake reads the SAME project + scene fakes, so a captured snapshot
reflects the project's live scenes and the current-pointer advance writes back
to the shared project fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.projects.project import Project
from tests.unit.application.use_cases.auth._fakes import (
    FakeProjectRepository,
    FakeProjectVersionRepository,
    FakeSceneRepository,
    FakeUnitOfWork,
)


def _dt() -> datetime:
    return datetime.now(UTC)


def make_project(*, tenant_id: UUID, owner_user_id: UUID) -> Project:
    """A minimal live project owned by ``(tenant_id, owner_user_id)``."""
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        folder_id=None,
        current_version_id=None,
        name=f"Project {uuid4().hex[:8]}",
        description=None,
        aspect_ratio="horizontal",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=_dt(),
        updated_at=_dt(),
        version=1,
    )


@dataclass
class Env:
    """The seeded fakes + the ids a test needs to address the owned project."""

    uow: FakeUnitOfWork
    projects: FakeProjectRepository
    scenes: FakeSceneRepository
    versions: FakeProjectVersionRepository
    owner_user_id: UUID
    tenant_id: UUID
    project_id: UUID


def build_env() -> Env:
    """Wire a UoW with one owned live project and an empty version ledger."""
    owner_user_id = uuid4()
    tenant_id = uuid4()
    project = make_project(tenant_id=tenant_id, owner_user_id=owner_user_id)
    projects = FakeProjectRepository(_rows={project.id: project})
    scenes = FakeSceneRepository()
    uow = FakeUnitOfWork(projects=projects, scenes=scenes)
    return Env(
        uow=uow,
        projects=projects,
        scenes=scenes,
        versions=uow._fake_versions,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        project_id=project.id,
    )


async def seed_scenes(env: Env, count: int) -> list[UUID]:
    """Append ``count`` scenes to ``env``'s project; return their ids in order."""
    storyboard_id, _ = await env.scenes.ensure_default_storyboard(env.project_id)
    ids: list[UUID] = []
    for i in range(count):
        scene = await env.scenes.add(
            storyboard_id=storyboard_id,
            title=f"Scene {i + 1}",
            duration_seconds=5.0,
            narration=None,
            subtitle=None,
        )
        ids.append(scene.id)
    return ids
