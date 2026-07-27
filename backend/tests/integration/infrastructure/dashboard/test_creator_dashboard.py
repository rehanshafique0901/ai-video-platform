"""α8.9c — Creator Dashboard end-to-end against live PostgreSQL (read-only).

Proves ``GET /api/v1/dashboard/summary`` aggregates the caller's real, committed,
owner-scoped state — publish-job counts by status, connected/total social accounts, the
unread notification count, and the media total — through the **reused** owner-scoped
repository reads. Also proves owner isolation (a fresh user sees all-zero) and the auth gate.

Like the publishing / notifications slices, the read stack + seeded writers commit their own
UoWs, so these tests seed committed rows and clean them up on teardown (FK-safe order) rather
than leaning on the SAVEPOINT ``session`` fixture. Nothing new is added to any subsystem — the
dashboard only composes existing reads.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import container
from app.domain.publishing.content_package import ContentPackage, Visibility
from app.domain.publishing.publish_status import PublishStatus
from app.infrastructure.db.models.jobs import ExportJob as ExportJobRow, RenderJob as RenderJobRow
from app.infrastructure.db.models.media import MediaAsset as MediaAssetRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.publishing import (
    PublishJob as PublishJobRow,
    SocialAccount as SocialAccountRow,
)
from app.infrastructure.db.models.timeline import Timeline as TimelineRow

pytestmark = pytest.mark.integration


async def _register(client: AsyncClient) -> dict[str, object]:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"dash-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="Dash Owner",
    )
    return {
        "user_id": result.user.id,
        "tenant_id": result.tenant.id,
        "access": result.tokens.access_token,
    }


def _content_package(media_id: UUID) -> dict:
    return ContentPackage(
        media_asset_id=media_id,
        title="t",
        description="d",
        tags=(),
        visibility=Visibility.PRIVATE,
        thumbnail_media_asset_id=None,
        publish_at=None,
    ).to_dict()


async def _media_row(s: AsyncSession, *, tenant_id: UUID, owner_user_id: UUID, tag: str) -> UUID:
    media_id = uuid4()
    await s.execute(
        insert(MediaAssetRow).values(
            id=media_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            kind="video",
            storage_backend="local",
            storage_bucket="media",
            storage_key=f"{tag}/{media_id}.mp4",
            mime_type="video/mp4",
            size_bytes=4096,
            checksum_sha256=b"\x00" * 32,
            source="generated",
        )
    )
    return media_id


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], scope: dict[str, object]
) -> None:
    """Seed a committed, owner-scoped fixture: 2 media, 2 connected accounts, 3 unread
    notifications, and 2 publish jobs (queued + succeeded on distinct account pairs so the
    (media, account) idempotency index is respected)."""
    tenant_id = scope["tenant_id"]
    user_id = scope["user_id"]
    async with session_factory() as s:
        project_id = uuid4()
        await s.execute(
            insert(ProjectRow).values(
                id=project_id,
                tenant_id=tenant_id,
                owner_user_id=user_id,
                name="Dash Project",
                aspect_ratio="horizontal",
            )
        )
        timeline_id = uuid4()
        await s.execute(
            insert(TimelineRow).values(
                id=timeline_id,
                project_id=project_id,
                aspect_ratio="16:9",
                frame_rate=30,
                background_color="#000000",
            )
        )
        master_id = await _media_row(s, tenant_id=tenant_id, owner_user_id=user_id, tag="master")
        delivery_id = await _media_row(
            s, tenant_id=tenant_id, owner_user_id=user_id, tag="delivery"
        )
        render_job_id = uuid4()
        await s.execute(
            insert(RenderJobRow).values(
                id=render_job_id,
                project_id=project_id,
                timeline_id=timeline_id,
                pipeline="ffmpeg",
                pipeline_version="0.0.0",
                queue="normal",
                status="succeeded",
                output_media_asset_id=master_id,
            )
        )
        export_job_id = uuid4()
        await s.execute(
            insert(ExportJobRow).values(
                id=export_job_id,
                render_job_id=render_job_id,
                requested_by_user_id=user_id,
                format="mp4",
                quality="hd_1080p",
                orientation="horizontal",
                status="succeeded",
                output_media_asset_id=delivery_id,
            )
        )
        account_ids = []
        for i in range(2):
            account_id = uuid4()
            await s.execute(
                insert(SocialAccountRow).values(
                    id=account_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    platform="youtube",
                    external_account_id=f"chan-{account_id}",
                    display_name=f"Chan {i}",
                    status="connected",
                    scopes=["publish"],
                )
            )
            account_ids.append(account_id)

        for account_id, job_status in (
            (account_ids[0], PublishStatus.QUEUED.value),
            (account_ids[1], PublishStatus.SUCCEEDED.value),
        ):
            await s.execute(
                insert(PublishJobRow).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    requested_by_user_id=user_id,
                    project_id=project_id,
                    source_export_job_id=export_job_id,
                    source_media_asset_id=delivery_id,
                    social_account_id=account_id,
                    platform="youtube",
                    status=job_status,
                    content_package=_content_package(delivery_id),
                    max_attempts=5,
                )
            )
        await s.commit()

    # 3 unread notifications via the reused committing writer.
    create = container.get_create_notification_use_case()
    for _ in range(3):
        await create.execute(
            user_id=user_id,
            kind="publish.succeeded",
            title="Your video was published",
            body="Your video was published to youtube.",
            payload={},
            source_event_id=uuid4(),
        )


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession], scope: dict[str, object]
) -> None:
    tenant_id = scope["tenant_id"]
    user_id = scope["user_id"]
    async with session_factory() as s:
        # FK-safe order: publish_jobs (media RESTRICT) → export/render (media RESTRICT) →
        # timelines/media/projects → user (cascades accounts/notifications/sessions) → tenant.
        for stmt in (
            "DELETE FROM publish_jobs WHERE requested_by_user_id = CAST(:u AS uuid)",
            "DELETE FROM export_jobs WHERE requested_by_user_id = CAST(:u AS uuid)",
            "DELETE FROM render_jobs WHERE project_id IN "
            "(SELECT id FROM projects WHERE owner_user_id = CAST(:u AS uuid))",
            "DELETE FROM timelines WHERE project_id IN "
            "(SELECT id FROM projects WHERE owner_user_id = CAST(:u AS uuid))",
            "DELETE FROM media_assets WHERE owner_user_id = CAST(:u AS uuid)",
            "DELETE FROM projects WHERE owner_user_id = CAST(:u AS uuid)",
            "DELETE FROM users WHERE id = CAST(:u AS uuid)",
        ):
            await s.execute(text(stmt), {"u": str(user_id)})
        await s.execute(
            text("DELETE FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": str(tenant_id)}
        )
        await s.commit()


async def test_summary_aggregates_committed_owner_state(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    scope = await _register(client)
    auth = {"Authorization": f"Bearer {scope['access']}"}
    try:
        await _seed(session_factory, scope)

        r = await client.get("/api/v1/dashboard/summary", headers=auth)
        assert r.status_code == 200, r.text
        data = r.json()["data"]

        assert data["publish_jobs"]["total"] == 2
        assert data["publish_jobs"]["queued"] == 1
        assert data["publish_jobs"]["succeeded"] == 1
        assert data["publish_jobs"]["running"] == 0
        assert data["publish_jobs"]["failed"] == 0
        assert data["publish_jobs"]["canceled"] == 0

        assert data["social_accounts"]["connected"] == 2
        assert data["social_accounts"]["total"] == 2

        assert data["notifications"]["unread"] == 3
        assert data["media"]["total"] == 2
    finally:
        await _cleanup(session_factory, scope)


async def test_fresh_user_sees_all_zero(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    scope = await _register(client)
    auth = {"Authorization": f"Bearer {scope['access']}"}
    try:
        r = await client.get("/api/v1/dashboard/summary", headers=auth)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["publish_jobs"] == {
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "canceled": 0,
            "total": 0,
        }
        assert data["social_accounts"] == {"connected": 0, "total": 0}
        assert data["notifications"] == {"unread": 0}
        assert data["media"] == {"total": 0}
    finally:
        await _cleanup(session_factory, scope)


async def test_summary_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/dashboard/summary")).status_code == 401
