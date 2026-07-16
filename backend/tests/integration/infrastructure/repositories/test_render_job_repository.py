"""Integration tests for ``RenderJobRepository`` + ``EventOutboxRepository`` (α7.1).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls
back on teardown, so no rows persist. Covers the project-scoped, self-versioned
render-job aggregate (ADR-0039): ``add`` (``version = 1``, DB defaults), the
``(project_id, idempotency_key)`` uniqueness backstop (→ ``ConflictError``),
newest-first listing with a ``status`` filter, project-scoped single reads, and
the version-fenced cancel CAS with its race-safe ``queued``/``running`` terminal
guard. Also exercises the transactional-outbox writer.

Coverage map (α7.1 pre-flight §5.4):

* R1  — ``add`` creates ``version = 1``, ``status = queued``, ``progress`` default.
* R2  — ``add`` duplicate ``(project_id, idempotency_key)`` → ``ConflictError``.
* R3  — ``add`` same key under a DIFFERENT project is allowed (scoping).
* R4  — ``get_by_project_and_key`` returns the row / ``None`` for a foreign project.
* R5  — ``list_by_project`` newest-first; ``status`` filter narrows.
* R6  — ``get_owned`` project isolation → ``None``.
* R7  — ``cancel`` happy: ``queued`` → ``canceled``, net ``+1`` version.
* R8  — ``cancel`` stale version → ``None`` (no write).
* R9  — ``cancel`` terminal state (``succeeded``) → ``None`` (race-safe guard).
* R10 — ``EventOutboxRepository.add`` persists an ``event_outbox`` row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.render.render_status import RenderStatus
from app.infrastructure.db.models.events import EventOutbox as EventOutboxRow
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.jobs import RenderJob as RenderJobRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.timeline import Timeline as TimelineRow
from app.infrastructure.repositories.event_outbox_repository import EventOutboxRepository
from app.infrastructure.repositories.render_job_repository import RenderJobRepository


async def _seed_project_with_timeline(session: AsyncSession) -> tuple[UUID, UUID]:
    """Seed tenant + user + project + timeline; return (project_id, timeline_id)."""
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="RJ Test", slug=f"rj-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"rj-{user_id}@example.com",
            display_name="RJ Owner",
        )
    )
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            name=f"P {project_id}",
            aspect_ratio="horizontal",
        )
    )
    timeline_id = uuid4()
    await session.execute(
        insert(TimelineRow).values(
            id=timeline_id,
            project_id=project_id,
            aspect_ratio="16:9",
            frame_rate=30,
            background_color="#000000",
        )
    )
    await session.flush()
    return project_id, timeline_id


async def _add_job(
    repo: RenderJobRepository,
    project_id: UUID,
    timeline_id: UUID,
    *,
    status: str = RenderStatus.QUEUED.value,
    queue: str = "normal",
    priority: int = 0,
    idempotency_key: str | None = None,
):  # type: ignore[no-untyped-def]
    return await repo.add(
        project_id=project_id,
        timeline_id=timeline_id,
        pipeline="ffmpeg",
        pipeline_version="0.0.0",
        queue=queue,
        priority=priority,
        status=status,
        idempotency_key=idempotency_key,
    )


# ---- R1 — add creates version 1 ---------------------------------------


@pytest.mark.integration
async def test_r1_add_creates_version_1(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)

    job = await _add_job(repo, project_id, timeline_id)
    assert job.version == 1
    assert job.status == RenderStatus.QUEUED.value
    assert job.project_id == project_id
    assert job.timeline_id == timeline_id
    assert job.progress == "0.00"
    assert job.error is None


# ---- R2 — duplicate idempotency key → 409 -----------------------------


@pytest.mark.integration
async def test_r2_duplicate_idempotency_key_conflicts(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)
    await _add_job(repo, project_id, timeline_id, idempotency_key="dup")

    with pytest.raises(ConflictError):
        await _add_job(repo, project_id, timeline_id, idempotency_key="dup")


# ---- R3 — same key under a different project is allowed ----------------


@pytest.mark.integration
async def test_r3_same_key_distinct_projects_allowed(session: AsyncSession) -> None:
    p1, t1 = await _seed_project_with_timeline(session)
    p2, t2 = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)

    j1 = await _add_job(repo, p1, t1, idempotency_key="shared")
    j2 = await _add_job(repo, p2, t2, idempotency_key="shared")
    assert j1.id != j2.id


# ---- R4 — get_by_project_and_key --------------------------------------


@pytest.mark.integration
async def test_r4_get_by_project_and_key(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)
    job = await _add_job(repo, project_id, timeline_id, idempotency_key="k")

    found = await repo.get_by_project_and_key(project_id, "k")
    assert found is not None and found.id == job.id

    foreign = await repo.get_by_project_and_key(uuid4(), "k")
    assert foreign is None


# ---- R5 — list_by_project newest-first + status filter ----------------


@pytest.mark.integration
async def test_r5_list_newest_first_and_status_filter(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)
    first = await _add_job(repo, project_id, timeline_id)
    second = await _add_job(repo, project_id, timeline_id)

    # ``created_at`` defaults to ``now()``, which Postgres holds CONSTANT for the
    # whole transaction — so both rows tie on ``created_at`` and the
    # ``created_at DESC, id DESC`` order would fall back to a nondeterministic
    # ``uuid4`` tiebreak. Push ``first`` into the past so "newest-first"
    # deterministically reflects insertion order. (This UPDATE leaves ``version``
    # untouched, so the ``bump_version`` trigger fires — re-read it before cancel.)
    await session.execute(
        update(RenderJobRow)
        .where(RenderJobRow.id == first.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await session.flush()

    all_jobs = await repo.list_by_project(project_id)
    assert [j.id for j in all_jobs] == [second.id, first.id]

    # Cancel the first, then filter by canceled (re-read version after the bump).
    refetched_first = await repo.get_owned(project_id, first.id)
    assert refetched_first is not None
    await repo.cancel(project_id, first.id, refetched_first.version)
    canceled = await repo.list_by_project(project_id, status=RenderStatus.CANCELED.value)
    assert [j.id for j in canceled] == [first.id]


# ---- R6 — get_owned project isolation → None --------------------------


@pytest.mark.integration
async def test_r6_get_owned_project_isolation(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)
    job = await _add_job(repo, project_id, timeline_id)

    assert (await repo.get_owned(project_id, job.id)) is not None
    assert (await repo.get_owned(uuid4(), job.id)) is None


# ---- R7 — cancel happy: net +1 ----------------------------------------


@pytest.mark.integration
async def test_r7_cancel_happy(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)
    job = await _add_job(repo, project_id, timeline_id)

    canceled = await repo.cancel(project_id, job.id, job.version)
    assert canceled is not None
    assert canceled.status == RenderStatus.CANCELED.value
    assert canceled.version == job.version + 1


# ---- R8 — cancel stale version → None ---------------------------------


@pytest.mark.integration
async def test_r8_cancel_stale_version(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)
    job = await _add_job(repo, project_id, timeline_id)

    assert (await repo.cancel(project_id, job.id, job.version + 99)) is None
    # Row unchanged.
    still = await repo.get_owned(project_id, job.id)
    assert still is not None and still.status == RenderStatus.QUEUED.value


# ---- R9 — cancel terminal state → None (race-safe guard) --------------


@pytest.mark.integration
async def test_r9_cancel_terminal_state(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)
    job = await _add_job(repo, project_id, timeline_id)

    # Simulate a worker completing the job (no worker in α7.1 — set state directly).
    await session.execute(
        update(RenderJobRow)
        .where(RenderJobRow.id == job.id)
        .values(status=RenderStatus.SUCCEEDED.value)
    )
    await session.flush()

    current = await repo.get_owned(project_id, job.id)
    assert current is not None
    # The CAS predicate's terminal-state guard yields no row → None.
    assert (await repo.cancel(project_id, job.id, current.version)) is None


# ---- R10 — outbox writer persists a row -------------------------------


@pytest.mark.integration
async def test_r10_outbox_add_persists_row(session: AsyncSession) -> None:
    project_id, timeline_id = await _seed_project_with_timeline(session)
    repo = RenderJobRepository(session)
    job = await _add_job(repo, project_id, timeline_id)

    outbox = EventOutboxRepository(session)
    await outbox.add(
        aggregate_type="render_job",
        aggregate_id=job.id,
        event_type="RenderJobCreated",
        payload={"render_job_id": str(job.id), "status": job.status},
        occurred_at=datetime.now(UTC),
        metadata={"actor_user_id": str(uuid4())},
    )
    await session.flush()

    count = (
        await session.execute(
            select(func.count())
            .select_from(EventOutboxRow)
            .where(EventOutboxRow.aggregate_id == job.id)
        )
    ).scalar_one()
    assert count == 1
