"""Shared scaffolding for the α6.2 Media use-case unit tests.

Media is **owner-scoped**, not project-scoped: the use cases gate directly on
``(tenant_id, owner_user_id)`` against the media fake — there is no project route
gate (contrast α6.1). But the *optional links* still validate against the sibling
project / scene / prompt fakes, so ``build_env`` wires an owned live
:class:`Project` (available for link tests) plus empty scene / prompt / media
fakes, and returns the fakes so tests can assert on repo state + ``commit``
bookkeeping. Helpers seed a scene / prompt (for link tests) and register a
linkable ``model_id``. ``media_kwargs`` returns a full register payload with a
**unique** storage key each call (so the storage-coordinate uniqueness
constraint only trips when a test deliberately reuses coords).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.domain.projects.project import Project
from tests.unit.application.use_cases.auth._fakes import (
    FakeMediaRepository,
    FakeProjectRepository,
    FakePromptRepository,
    FakeSceneRepository,
    FakeUnitOfWork,
)

_KEY_SEQ = itertools.count(1)

# A deterministic 32-byte sha256 digest (64 hex chars) for register payloads.
_CHECKSUM = bytes.fromhex("ab" * 32)


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
    """The seeded fakes + the ids a test needs to address the owner + project."""

    uow: FakeUnitOfWork
    projects: FakeProjectRepository
    scenes: FakeSceneRepository
    prompts: FakePromptRepository
    media: FakeMediaRepository
    owner_user_id: UUID
    tenant_id: UUID
    project_id: UUID


def build_env() -> Env:
    """Wire a UoW with one owned live project + empty scene/prompt/media repos."""
    owner_user_id = uuid4()
    tenant_id = uuid4()
    project = make_project(tenant_id=tenant_id, owner_user_id=owner_user_id)
    projects = FakeProjectRepository(_rows={project.id: project})
    scenes = FakeSceneRepository()
    prompts = FakePromptRepository()
    media = FakeMediaRepository()
    uow = FakeUnitOfWork(projects=projects, scenes=scenes, prompts=prompts, media=media)
    return Env(
        uow=uow,
        projects=projects,
        scenes=scenes,
        prompts=prompts,
        media=media,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        project_id=project.id,
    )


def media_kwargs(**overrides: Any) -> dict[str, Any]:
    """A complete ``RegisterMedia.execute`` payload with a unique storage key.

    Excludes ``owner_user_id`` / ``tenant_id`` (server-owned — the test supplies
    them from ``env``). Override any field via kwargs.
    """
    payload: dict[str, Any] = {
        "kind": "image",
        "source": "uploaded",
        "storage_backend": "s3",
        "storage_bucket": "assets",
        "storage_key": f"uploads/{next(_KEY_SEQ)}.png",
        "mime_type": "image/png",
        "size_bytes": 2048,
        "checksum_sha256": _CHECKSUM,
    }
    payload.update(overrides)
    return payload


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


async def seed_prompt(env: Env) -> UUID:
    """Add one prompt to ``env``'s project; return its id (for prompt-link tests)."""
    prompt = await env.prompts.add(
        project_id=env.project_id,
        scene_id=None,
        kind="image",
        text_content="x",
        model_id=None,
        extra={},
    )
    return prompt.id


def register_model(env: Env) -> UUID:
    """Register a linkable ``model_id`` in the media fake; return it."""
    model_id = uuid4()
    env.media._linkable_models.add(model_id)
    return model_id
