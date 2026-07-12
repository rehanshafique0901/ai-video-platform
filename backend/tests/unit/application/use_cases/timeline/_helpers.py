"""Shared scaffolding for the α6.3a Timeline + Track use-case unit tests.

The Timeline is a **project-nested** aggregate (contrast the owner-scoped media):
every use case runs a two-level gate — project ownership (→ 404) then timeline
resolution (→ 404) — before touching the aggregate. ``build_env`` wires a UoW
with one owned live :class:`Project` plus an empty timeline fake, and returns the
fakes so tests can assert on repo state + ``commit`` bookkeeping. ``seed_timeline``
provisions the single timeline; ``seed_track`` appends a track. No timeline
mutation ever bumps ``projects.version`` (ADR-0035 / ADR-0038).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.projects.project import Project
from app.domain.timeline.timeline import Timeline
from app.domain.timeline.track import Track
from tests.unit.application.use_cases.auth._fakes import (
    FakeProjectRepository,
    FakeTimelineRepository,
    FakeUnitOfWork,
)


def _dt() -> datetime:
    return datetime.now(UTC)


def make_project(
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    aspect_ratio: str = "horizontal",
) -> Project:
    """A minimal live project owned by ``(tenant_id, owner_user_id)``."""
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        folder_id=None,
        current_version_id=None,
        name=f"Project {uuid4().hex[:8]}",
        description=None,
        aspect_ratio=aspect_ratio,
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
    timeline: FakeTimelineRepository
    owner_user_id: UUID
    tenant_id: UUID
    project_id: UUID


def build_env(*, aspect_ratio: str = "horizontal") -> Env:
    """Wire a UoW with one owned live project + an empty timeline repo."""
    owner_user_id = uuid4()
    tenant_id = uuid4()
    project = make_project(
        tenant_id=tenant_id, owner_user_id=owner_user_id, aspect_ratio=aspect_ratio
    )
    projects = FakeProjectRepository(_rows={project.id: project})
    timeline = FakeTimelineRepository()
    uow = FakeUnitOfWork(projects=projects, timeline=timeline)
    return Env(
        uow=uow,
        projects=projects,
        timeline=timeline,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        project_id=project.id,
    )


async def seed_timeline(
    env: Env,
    *,
    aspect_ratio: str = "16:9",
    frame_rate: int = 30,
    background_color: str = "#000000",
) -> Timeline:
    """Provision the single timeline for ``env``'s project (bypassing the use case)."""
    return await env.timeline.add(
        project_id=env.project_id,
        aspect_ratio=aspect_ratio,
        frame_rate=frame_rate,
        background_color=background_color,
    )


async def seed_track(
    env: Env,
    timeline: Timeline,
    *,
    kind: str = "video",
    z_index: int = 0,
    name: str = "Track",
    locked: bool = False,
    muted: bool = False,
) -> Track:
    """Append one track to ``timeline`` (bypassing the use case)."""
    return await env.timeline.add_track(
        timeline_id=timeline.id,
        kind=kind,
        z_index=z_index,
        name=name,
        locked=locked,
        muted=muted,
    )
