"""Shared scaffolding for the α6.1 Prompt use-case unit tests.

Every prompt use case runs the **two-level visibility gate** (α6.1 D2): it
first calls ``projects.get_owned`` and 404s if the caller does not own a live
project, then reaches the prompt port. So every test needs a seeded, owned
:class:`Project` in the ``FakeProjectRepository``. ``build_env`` wires that up
(plus empty scene + prompt fakes) and returns the fakes so tests can assert on
repo state and ``commit`` bookkeeping. Helpers seed a scene (for scene-link
tests) and register a linkable ``model_id`` (for model-link tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.projects.project import Project
from tests.unit.application.use_cases.auth._fakes import (
    FakeProjectRepository,
    FakePromptRepository,
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
    prompts: FakePromptRepository
    owner_user_id: UUID
    tenant_id: UUID
    project_id: UUID


def build_env() -> Env:
    """Wire a UoW with one owned live project and empty scene + prompt repos."""
    owner_user_id = uuid4()
    tenant_id = uuid4()
    project = make_project(tenant_id=tenant_id, owner_user_id=owner_user_id)
    projects = FakeProjectRepository(_rows={project.id: project})
    scenes = FakeSceneRepository()
    prompts = FakePromptRepository()
    uow = FakeUnitOfWork(projects=projects, scenes=scenes, prompts=prompts)
    return Env(
        uow=uow,
        projects=projects,
        scenes=scenes,
        prompts=prompts,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        project_id=project.id,
    )


async def seed_scene(env: Env) -> UUID:
    """Append one scene to ``env``'s project; return its id (for scene-link tests)."""
    storyboard_id, _ = await env.scenes.ensure_default_storyboard(env.project_id)
    scene = await env.scenes.add(
        storyboard_id=storyboard_id,
        title="Scene 1",
        duration_seconds=5.0,
        narration=None,
        subtitle=None,
    )
    return scene.id


def register_model(env: Env) -> UUID:
    """Register a linkable ``model_id`` in the prompt fake; return it."""
    model_id = uuid4()
    env.prompts._linkable_models.add(model_id)
    return model_id
