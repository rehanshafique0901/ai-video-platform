"""Integration tests for Publish Thumbnail Support (Slice α9.3, ADR-0050).

Drives the **real** publish stack against the live database inside a SAVEPOINT that rolls
back on teardown to prove the additive, best-effort thumbnail path end-to-end:

* **T1 — carry-through:** a create that nominates the caller's own *image* asset captures the
  id immutably into the ``ContentPackage``; the worker resolves + materialises it (owner-scoped,
  in the worker — never the adapter) and hands the adapter an ``UploadMedia`` carrying an
  ``UploadThumbnail``; the video still publishes to ``succeeded``.
* **T2 — best-effort:** if the nominated image is soft-deleted after queuing but before the
  worker runs, the video still publishes to ``succeeded`` and the adapter receives NO thumbnail
  (ADR-0050 Invariants 2/7 — the thumbnail is advisory and never blocks the primary publish).
* **T3 — ownership 404 / T4 — kind 422:** ``CreatePublishJob`` verifies the thumbnail is the
  caller's own image *before* the job is queued (a non-owned id → 404; a non-image → 422).

Plus two HTTP-contract checks through the real ``/api/v1/publish-jobs`` ingress (the create
happy path needs a CONNECTED account the client fixture cannot roll back — proven above via the
session-bound stack — so these assert only the additive schema field parses / is validated).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.destination_publisher import IDestinationPublisher, PublishResult
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.application.use_cases.publishing.create_publish_job import CreatePublishJob
from app.application.use_cases.publishing.process_publish_job import ProcessPublishJob
from app.application.use_cases.publishing.publish_worker import PublishWorker
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.export.export_status import ExportStatus
from app.domain.publishing.publish_status import PublishStatus
from app.domain.render.render_status import RenderStatus
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.jobs import ExportJob as ExportJobRow, RenderJob as RenderJobRow
from app.infrastructure.db.models.media import MediaAsset as MediaAssetRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.timeline import Timeline as TimelineRow
from app.infrastructure.publishing.destinations.mock_destination import MockDestination
from app.infrastructure.publishing.destinations.registry import DestinationRegistry
from app.infrastructure.repositories.distributed_lock_manager import (
    SqlAlchemyDistributedLockManager,
)
from app.infrastructure.repositories.event_outbox_repository import EventOutboxRepository
from app.infrastructure.repositories.media_repository import MediaRepository
from app.infrastructure.repositories.project_repository import ProjectRepository
from app.infrastructure.repositories.publish_job_repository import PublishJobRepository
from app.infrastructure.repositories.social_account_repository import SocialAccountRepository

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Session-bound Unit of Work (writes stay inside the test SAVEPOINT)          #
# --------------------------------------------------------------------------- #
class _PublishUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.publish_jobs = PublishJobRepository(session)
        self.social_accounts = SocialAccountRepository(session)
        self.projects = ProjectRepository(session)
        self.media = MediaRepository(session)
        self.outbox = EventOutboxRepository(session)
        self.locks = SqlAlchemyDistributedLockManager(session)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        await self._session.flush()

    async def rollback(self) -> None:
        await self._session.flush()


class _StubCredentialStore:
    def __init__(self, token: str = "bearer-thumb") -> None:
        self._token = token
        self.authorized: list[UUID] = []

    async def store(self, social_account_id, tokens):  # pragma: no cover
        return None

    async def authorize(self, social_account_id: UUID) -> AuthorizedContext:
        self.authorized.append(social_account_id)
        return AuthorizedContext(
            access_token=self._token, expires_at=datetime.now(UTC), scopes=("publish",)
        )

    async def revoke(self, social_account_id):  # pragma: no cover
        return None


class _InMemoryObjectStorage:
    def __init__(self, bucket: str, data: bytes) -> None:
        self._bucket = bucket
        self._data = data

    @property
    def bucket(self) -> str:
        return self._bucket

    async def get(self, *, key: str) -> bytes:
        return self._data


class _StorageResolver:
    def __init__(self, storage: _InMemoryObjectStorage) -> None:
        self._storage = storage

    def active(self):  # pragma: no cover
        return self._storage

    def resolve(self, backend: str) -> _InMemoryObjectStorage:
        return self._storage


class _RecordingMockDestination(MockDestination):
    """A real MockDestination that records the ``UploadMedia`` it received (α9.3)."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_thumbnail_is_none = True
        self.seen_thumbnail_mime: str | None = None

    async def publish(self, *, package, auth, media) -> PublishResult:
        self.seen_thumbnail_is_none = media.thumbnail is None
        self.seen_thumbnail_mime = None if media.thumbnail is None else media.thumbnail.mime_type
        return await super().publish(package=package, auth=auth, media=media)


# --------------------------------------------------------------------------- #
# Seed — a finished export delivery + a connected account + an owned image     #
# --------------------------------------------------------------------------- #
async def _seed(session: AsyncSession) -> dict[str, UUID]:
    tenant_id = uuid4()
    await session.execute(insert(Tenant).values(id=tenant_id, name="Thumb", slug=f"th-{tenant_id}"))
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"th-{user_id}@example.com",
            display_name="Thumb Owner",
        )
    )
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            name="Thumb Video",
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
    master_id, delivery_id = uuid4(), uuid4()
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
                size_bytes=4096,
                checksum_sha256=b"\x00" * 32,
                source="generated",
            )
        )
    thumbnail_id = uuid4()
    await session.execute(
        insert(MediaAssetRow).values(
            id=thumbnail_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            kind="image",
            storage_backend="local",
            storage_bucket="media",
            storage_key=f"thumbs/{thumbnail_id}.jpg",
            mime_type="image/jpeg",
            size_bytes=1024,
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
            status=ExportStatus.SUCCEEDED.value,
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
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "export_job_id": export_job_id,
        "delivery_id": delivery_id,
        "thumbnail_id": thumbnail_id,
        "social_account_id": account.id,
    }


def _runtime(uow: _PublishUnitOfWork, destination: IDestinationPublisher) -> PublishWorker:
    resolver = _StorageResolver(_InMemoryObjectStorage("media", b"\x01\x02\x03\x04"))
    return PublishWorker(
        uow,  # type: ignore[arg-type]
        ProcessPublishJob(uow, resolver, _StubCredentialStore(), DestinationRegistry({"mock": destination})),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# T1 — thumbnail carried through to the adapter, publish succeeds              #
# --------------------------------------------------------------------------- #
async def test_thumbnail_carried_through_to_adapter(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    dest = _RecordingMockDestination()

    create = CreatePublishJob(uow, supported_platforms={"mock"})  # type: ignore[arg-type]
    created = await create.execute(
        owner_user_id=ids["user_id"],
        tenant_id=ids["tenant_id"],
        export_job_id=ids["export_job_id"],
        social_account_id=ids["social_account_id"],
        thumbnail_media_asset_id=ids["thumbnail_id"],
    )
    assert created.created is True
    assert created.job.content_package.thumbnail_media_asset_id == ids["thumbnail_id"]

    poll = await _runtime(uow, dest).run_once()
    assert [o.status for o in poll.outcomes] == ["published"]
    # Worker resolved + materialised the owned image; the adapter received it (adapter is blind).
    assert dest.seen_thumbnail_is_none is False
    assert dest.seen_thumbnail_mime == "image/jpeg"

    settled = await uow.publish_jobs.get_owned(
        tenant_id=ids["tenant_id"], owner_user_id=ids["user_id"], publish_job_id=created.job.id
    )
    assert settled is not None
    assert settled.status == PublishStatus.SUCCEEDED.value
    assert settled.content_package.thumbnail_media_asset_id == ids["thumbnail_id"]


# --------------------------------------------------------------------------- #
# T2 — best-effort: image gone at publish time → publish still succeeds        #
# --------------------------------------------------------------------------- #
async def test_best_effort_when_thumbnail_deleted_before_publish(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    dest = _RecordingMockDestination()

    create = CreatePublishJob(uow, supported_platforms={"mock"})  # type: ignore[arg-type]
    created = await create.execute(
        owner_user_id=ids["user_id"],
        tenant_id=ids["tenant_id"],
        export_job_id=ids["export_job_id"],
        social_account_id=ids["social_account_id"],
        thumbnail_media_asset_id=ids["thumbnail_id"],
    )

    # Creator soft-deletes the nominated image after queuing but before the worker runs.
    removed = await uow.media.soft_delete_owned(
        ids["thumbnail_id"], ids["tenant_id"], ids["user_id"]
    )
    assert removed is True

    poll = await _runtime(uow, dest).run_once()
    assert [o.status for o in poll.outcomes] == ["published"]
    assert dest.seen_thumbnail_is_none is True  # advisory — never blocks the video publish

    settled = await uow.publish_jobs.get_owned(
        tenant_id=ids["tenant_id"], owner_user_id=ids["user_id"], publish_job_id=created.job.id
    )
    assert settled is not None
    assert settled.status == PublishStatus.SUCCEEDED.value
    # The immutable package still references the (now-deleted) image id.
    assert settled.content_package.thumbnail_media_asset_id == ids["thumbnail_id"]


# --------------------------------------------------------------------------- #
# T3 — non-owned thumbnail is 404 (verified before the job is queued)          #
# --------------------------------------------------------------------------- #
async def test_non_owned_thumbnail_is_404(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    create = CreatePublishJob(uow, supported_platforms={"mock"})  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        await create.execute(
            owner_user_id=ids["user_id"],
            tenant_id=ids["tenant_id"],
            export_job_id=ids["export_job_id"],
            social_account_id=ids["social_account_id"],
            thumbnail_media_asset_id=uuid4(),  # not the caller's
        )


# --------------------------------------------------------------------------- #
# T4 — non-image thumbnail is 422 (a video asset cannot be a thumbnail)        #
# --------------------------------------------------------------------------- #
async def test_non_image_thumbnail_is_422(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    create = CreatePublishJob(uow, supported_platforms={"mock"})  # type: ignore[arg-type]
    with pytest.raises(ValidationFailedError):
        await create.execute(
            owner_user_id=ids["user_id"],
            tenant_id=ids["tenant_id"],
            export_job_id=ids["export_job_id"],
            social_account_id=ids["social_account_id"],
            thumbnail_media_asset_id=ids["delivery_id"],  # a video, not an image
        )


# --------------------------------------------------------------------------- #
# HTTP contract — the additive schema field parses / is validated at ingress   #
# --------------------------------------------------------------------------- #
def _bearer(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> str:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"th-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "TH",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["access_token"]


async def test_api_non_uuid_thumbnail_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(
        "/api/v1/publish-jobs",
        headers=_bearer(access),
        json={
            "export_job_id": str(uuid4()),
            "social_account_id": str(uuid4()),
            "thumbnail_media_asset_id": "not-a-uuid",
        },
    )
    assert r.status_code == 422, r.text


async def test_api_valid_thumbnail_uuid_parses_then_404_on_unknown_account(
    client: AsyncClient,
) -> None:
    # A well-formed thumbnail id passes ingress validation, so the request proceeds to the
    # account gate and 404s on an unknown account (proving the additive field parses).
    access = await _register(client)
    r = await client.post(
        "/api/v1/publish-jobs",
        headers=_bearer(access),
        json={
            "export_job_id": str(uuid4()),
            "social_account_id": str(uuid4()),
            "thumbnail_media_asset_id": str(uuid4()),
        },
    )
    assert r.status_code == 404, r.text
