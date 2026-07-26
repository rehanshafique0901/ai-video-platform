"""Integration tests for ``PublishJobRepository`` (α8.6b).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls back on
teardown. Covers source resolution (PUB-1, owner-scoped), owner-scoped create/reads, the
(source_media_asset, social_account) idempotency backstop (DQ2), the version-fenced CAS
transitions (mark_running/succeeded/failed + reschedule, DQ8), and the due-only claim scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.domain.export.export_status import ExportStatus
from app.domain.publishing.content_package import build_content_package
from app.domain.publishing.publish_status import PublishStatus
from app.domain.render.render_status import RenderStatus
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.jobs import ExportJob as ExportJobRow, RenderJob as RenderJobRow
from app.infrastructure.db.models.media import MediaAsset as MediaAssetRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.timeline import Timeline as TimelineRow
from app.infrastructure.repositories.publish_job_repository import PublishJobRepository
from app.infrastructure.repositories.social_account_repository import SocialAccountRepository

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Prereqs:
    tenant_id: UUID
    user_id: UUID
    project_id: UUID
    export_job_id: UUID
    delivery_media_id: UUID
    social_account_id: UUID
    platform: str


async def _seed(
    session: AsyncSession, *, export_status: str = ExportStatus.SUCCEEDED.value
) -> _Prereqs:
    tenant_id = uuid4()
    await session.execute(insert(Tenant).values(id=tenant_id, name="PJ", slug=f"pj-{tenant_id}"))
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"pj-{user_id}@example.com",
            display_name="PJ Owner",
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

    master_id = uuid4()
    delivery_id = uuid4()
    for mid, key in ((master_id, "master"), (delivery_id, "delivery")):
        await session.execute(
            insert(MediaAssetRow).values(
                id=mid,
                tenant_id=tenant_id,
                owner_user_id=user_id,
                kind="video",
                storage_backend="local",
                storage_bucket="media",
                storage_key=f"{key}/{mid}.mp4",
                mime_type="video/mp4",
                size_bytes=2048,
                checksum_sha256=b"\x00" * 32,
                source="generated",
            )
        )
    render_job_id = uuid4()
    await session.execute(
        insert(RenderJobRow).values(
            id=render_job_id,
            project_id=project_id,
            timeline_id=timeline_id,
            pipeline="ffmpeg",
            pipeline_version="0.0.0",
            queue="normal",
            status=RenderStatus.SUCCEEDED.value,
            output_media_asset_id=master_id,
        )
    )
    export_job_id = uuid4()
    await session.execute(
        insert(ExportJobRow).values(
            id=export_job_id,
            render_job_id=render_job_id,
            requested_by_user_id=user_id,
            format="mp4",
            quality="hd_1080p",
            orientation="horizontal",
            status=export_status,
            output_media_asset_id=delivery_id,
        )
    )
    await session.flush()

    account = await SocialAccountRepository(session).upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id=f"chan-{uuid4()}",
        display_name="Mock Channel",
        scopes=("publish",),
    )
    return _Prereqs(
        tenant_id=tenant_id,
        user_id=user_id,
        project_id=project_id,
        export_job_id=export_job_id,
        delivery_media_id=delivery_id,
        social_account_id=account.id,
        platform="mock",
    )


async def _add(repo: PublishJobRepository, p: _Prereqs):  # type: ignore[no-untyped-def]
    return await repo.add(
        tenant_id=p.tenant_id,
        requested_by_user_id=p.user_id,
        project_id=p.project_id,
        source_export_job_id=p.export_job_id,
        source_media_asset_id=p.delivery_media_id,
        social_account_id=p.social_account_id,
        platform=p.platform,
        status=PublishStatus.QUEUED.value,
        scheduled_at=None,
        content_package=build_content_package(
            media_asset_id=p.delivery_media_id, project_title="P"
        ),
        max_attempts=5,
    )


# ---- resolve_source (PUB-1) ------------------------------------------------


async def test_resolve_source_owner_scoped(session: AsyncSession) -> None:
    p = await _seed(session)
    repo = PublishJobRepository(session)
    src = await repo.resolve_source(p.export_job_id, tenant_id=p.tenant_id, owner_user_id=p.user_id)
    assert src is not None
    assert src.project_id == p.project_id
    assert src.source_media_asset_id == p.delivery_media_id
    assert src.export_status == ExportStatus.SUCCEEDED.value

    # A different principal cannot resolve it (anti-enumeration).
    assert (
        await repo.resolve_source(p.export_job_id, tenant_id=p.tenant_id, owner_user_id=uuid4())
        is None
    )


# ---- add / reads / idempotency ---------------------------------------------


async def test_add_creates_queued_job_with_defaults(session: AsyncSession) -> None:
    p = await _seed(session)
    repo = PublishJobRepository(session)
    job = await _add(repo, p)
    assert job.status == PublishStatus.QUEUED.value
    assert job.attempt == 0
    assert job.max_attempts == 5
    assert job.version == 1
    assert job.project_id == p.project_id
    assert job.content_package.media_asset_id == p.delivery_media_id


async def test_get_owned_is_owner_scoped(session: AsyncSession) -> None:
    p = await _seed(session)
    repo = PublishJobRepository(session)
    job = await _add(repo, p)
    assert (
        await repo.get_owned(tenant_id=p.tenant_id, owner_user_id=p.user_id, publish_job_id=job.id)
    ) is not None
    assert (
        await repo.get_owned(tenant_id=p.tenant_id, owner_user_id=uuid4(), publish_job_id=job.id)
    ) is None


async def test_idempotency_conflict_on_duplicate(session: AsyncSession) -> None:
    # The partial-unique backstop rejects a second active publish for the same
    # (delivery, account) (DQ2). A failed flush poisons the transaction, so — per the
    # repository-test convention — we assert the raise and stop; the "return the existing
    # job" recovery is proven by the create use case (unit) + the e2e idempotent replay.
    p = await _seed(session)
    repo = PublishJobRepository(session)
    await _add(repo, p)
    with pytest.raises(ConflictError):
        await _add(repo, p)


# ---- CAS transitions (DQ8) -------------------------------------------------


async def test_lifecycle_running_then_succeeded(session: AsyncSession) -> None:
    p = await _seed(session)
    repo = PublishJobRepository(session)
    job = await _add(repo, p)

    running = await repo.mark_running(job.id)
    assert running is not None
    assert running.status == PublishStatus.RUNNING.value
    assert running.attempt == 1
    assert running.version == 2  # explicit +1; guarded trigger no-ops

    # A second claim finds nothing to claim (not queued).
    assert await repo.mark_running(job.id) is None

    settled = await repo.mark_succeeded(
        job.id, platform_post_id="post-1", platform_post_url="https://x/post-1"
    )
    assert settled is not None
    assert settled.status == PublishStatus.SUCCEEDED.value
    assert settled.platform_post_id == "post-1"
    assert settled.published_at is not None
    assert settled.finished_at is not None
    assert settled.version == 3


async def test_reschedule_for_retry_requeues(session: AsyncSession) -> None:
    p = await _seed(session)
    repo = PublishJobRepository(session)
    job = await _add(repo, p)
    await repo.mark_running(job.id)
    when = datetime.now(UTC) + timedelta(minutes=5)
    requeued = await repo.reschedule_for_retry(
        job.id, scheduled_at=when, error={"code": "rate_limited", "message": "slow down"}
    )
    assert requeued is not None
    assert requeued.status == PublishStatus.QUEUED.value
    assert requeued.scheduled_at is not None
    assert requeued.error == {"code": "rate_limited", "message": "slow down"}


async def test_mark_failed_records_error(session: AsyncSession) -> None:
    p = await _seed(session)
    repo = PublishJobRepository(session)
    job = await _add(repo, p)
    await repo.mark_running(job.id)
    failed = await repo.mark_failed(job.id, error={"code": "rejected", "message": "no"})
    assert failed is not None
    assert failed.status == PublishStatus.FAILED.value
    assert failed.error == {"code": "rejected", "message": "no"}
    assert failed.finished_at is not None


# ---- claim scan (due-only) -------------------------------------------------


async def test_list_claimable_excludes_future_scheduled(session: AsyncSession) -> None:
    p = await _seed(session)
    repo = PublishJobRepository(session)
    job = await _add(repo, p)
    now = datetime.now(UTC)

    # Due now (scheduled_at NULL) → claimable.
    claims = await repo.list_claimable(now=now, limit=10)
    assert any(c.publish_job_id == job.id and c.project_id == p.project_id for c in claims)

    # Push it into the future via a retry → excluded until due.
    await repo.mark_running(job.id)
    await repo.reschedule_for_retry(
        job.id, scheduled_at=now + timedelta(hours=1), error={"code": "x", "message": "y"}
    )
    later_claims = await repo.list_claimable(now=now, limit=10)
    assert all(c.publish_job_id != job.id for c in later_claims)

    # Once the schedule passes, it is claimable again.
    due_claims = await repo.list_claimable(now=now + timedelta(hours=2), limit=10)
    assert any(c.publish_job_id == job.id for c in due_claims)
