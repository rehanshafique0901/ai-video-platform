"""Shared scaffolding for the α7.1 RenderJob use-case unit tests.

A RenderJob is a **project-nested, self-versioned orchestration** aggregate:
every use case runs the project ownership gate (→ 404) first, then works over
the ``render_jobs`` fake. ``build_env`` wires a UoW with one owned live
:class:`Project` plus (by default) its single :class:`Timeline` — a render job
needs a timeline to render — and empty render-job + outbox fakes, returning the
fakes so tests can assert repo state, emitted events, and ``commit`` bookkeeping.
``seed_render_job`` inserts a queued job (bypassing the use case).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.projects.project import Project
from app.domain.render.render_job import RenderJob
from app.domain.render.render_status import RenderStatus
from app.domain.timeline.timeline import Timeline
from tests.unit.application.use_cases.auth._fakes import (
    FakeEventOutboxRepository,
    FakeProjectRepository,
    FakeRenderJobRepository,
    FakeTimelineRepository,
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
    """The seeded fakes + the ids a test needs to address the owner + project."""

    uow: FakeUnitOfWork
    projects: FakeProjectRepository
    timeline: FakeTimelineRepository
    render_jobs: FakeRenderJobRepository
    outbox: FakeEventOutboxRepository
    owner_user_id: UUID
    tenant_id: UUID
    project_id: UUID
    timeline_id: UUID | None


def build_env(*, with_timeline: bool = True) -> Env:
    """Wire a UoW with one owned live project (+ its timeline unless suppressed).

    ``with_timeline=False`` models a project that has not been provisioned a
    timeline yet — used to exercise the ``422 has no timeline`` create path.
    """
    owner_user_id = uuid4()
    tenant_id = uuid4()
    project = make_project(tenant_id=tenant_id, owner_user_id=owner_user_id)
    projects = FakeProjectRepository(_rows={project.id: project})
    timeline_repo = FakeTimelineRepository()
    render_jobs = FakeRenderJobRepository()
    outbox = FakeEventOutboxRepository()
    uow = FakeUnitOfWork(
        projects=projects,
        timeline=timeline_repo,
        render_jobs=render_jobs,
        outbox=outbox,
    )

    timeline_id: UUID | None = None
    if with_timeline:
        # Seed synchronously via the fake's dict (add() is async; the fake stores
        # a live timeline keyed by project — build one directly to keep build_env
        # non-async, matching the timeline helpers' seed_* being the async ones).
        now = _dt()
        timeline = Timeline(
            id=uuid4(),
            project_id=project.id,
            project_version_id=None,
            duration_seconds=0.0,
            aspect_ratio="16:9",
            frame_rate=30,
            background_color="#000000",
            version=1,
            created_at=now,
            updated_at=now,
        )
        timeline_repo._timelines[timeline.id] = timeline
        timeline_id = timeline.id

    return Env(
        uow=uow,
        projects=projects,
        timeline=timeline_repo,
        render_jobs=render_jobs,
        outbox=outbox,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        project_id=project.id,
        timeline_id=timeline_id,
    )


async def seed_render_job(
    env: Env,
    *,
    status: str = RenderStatus.QUEUED.value,
    pipeline: str = "ffmpeg",
    pipeline_version: str = "0.0.0",
    queue: str = "normal",
    priority: int = 0,
    idempotency_key: str | None = None,
) -> RenderJob:
    """Insert one render job for ``env``'s project (bypassing the use case).

    Defaults to ``queued``; pass ``status`` to seed a running/terminal job for
    cancel-path tests. Requires ``env`` to have a timeline.
    """
    assert env.timeline_id is not None, "seed_render_job needs a timeline"
    job = await env.render_jobs.add(
        project_id=env.project_id,
        timeline_id=env.timeline_id,
        pipeline=pipeline,
        pipeline_version=pipeline_version,
        queue=queue,
        priority=priority,
        status=status,
        idempotency_key=idempotency_key,
    )
    return job
