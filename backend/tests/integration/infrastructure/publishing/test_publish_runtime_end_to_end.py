"""End-to-end integration test for the Publish Runtime (Slice α8.6b).

Drives the **real** publish stack against the live database inside a SAVEPOINT that rolls
back on teardown: :class:`CreatePublishJob` queues a publish of a finished export delivery,
then :class:`PublishWorker` (poll ingress) claims it under real ``distributed_locks`` leases
and :class:`ProcessPublishJob` authorizes (credential-blind), materializes the delivery
artifact, uploads via the credential-blind ``MockDestination``, and settles the job — all on
one session-bound Unit of Work, exercising the SQL the unit suite cannot (the ``publish_jobs``
source-resolution join, the queued→running→succeeded CAS with version bump, the claim scan,
and the transactional-outbox writes).

This is the α8.6b milestone check: a user's publish intent runs to a durable ``succeeded``
with a platform post identity + a ``PublishJobCreated`` → ``PublishJobSucceeded`` outbox chain,
with **no** credential material crossing the worker/adapter boundary (DQ3/PUB-5). Three flows:

* **P1 — happy path:** create → worker claims → Mock upload → ``succeeded`` (attempt 1),
  ``PublishJobCreated`` + ``PublishJobSucceeded`` emitted, both leases released.
* **P2 — idempotent replay:** a repeat create for the same (delivery, account) returns the
  existing job (no second row, no second event) (DQ2).
* **P3 — permanent failure:** a destination that rejects → ``failed`` with a neutral error +
  a single ``PublishJobFailed`` event, no retry (DQ6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.destination_publisher import (
    DestinationError,
    IDestinationPublisher,
    PublishResult,
)
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.application.use_cases.publishing.create_publish_job import CreatePublishJob
from app.application.use_cases.publishing.process_publish_job import ProcessPublishJob
from app.application.use_cases.publishing.publish_worker import PublishWorker
from app.domain.export.export_status import ExportStatus
from app.domain.publishing.publish_status import PublishStatus
from app.domain.render.render_status import RenderStatus
from app.infrastructure.db.models.events import EventOutbox
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


# --------------------------------------------------------------------------- #
# Credential-blind + storage doubles (proven elsewhere; here they are seams)  #
# --------------------------------------------------------------------------- #
class _StubCredentialStore:
    """Hands out a ready bearer (DQ3): the runtime consumes only an AuthorizedContext."""

    def __init__(self, token: str = "bearer-e2e") -> None:
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


class _RejectingDestination(IDestinationPublisher):
    @property
    def platform(self) -> str:
        return "mock"

    async def publish(self, *, package, auth, media) -> PublishResult:
        raise DestinationError("permanently rejected", retryable=False, code="rejected")


# --------------------------------------------------------------------------- #
# Seed                                                                        #
# --------------------------------------------------------------------------- #
async def _seed(session: AsyncSession) -> dict[str, UUID | str]:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="PubE2E", slug=f"pe-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"pe-{user_id}@example.com",
            display_name="Pub Owner",
        )
    )
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            name="Launch Video",
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
        "project_id": project_id,
        "export_job_id": export_job_id,
        "delivery_id": delivery_id,
        "social_account_id": account.id,
    }


async def _outbox_events(session: AsyncSession, publish_job_id: UUID) -> list[str]:
    stmt = (
        select(EventOutbox.event_type)
        .where(EventOutbox.aggregate_type == "publish_job")
        .where(EventOutbox.aggregate_id == publish_job_id)
        .order_by(EventOutbox.occurred_at, EventOutbox.id)
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


async def _lock_count(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(_distributed_locks()))).scalar_one()
    )


def _distributed_locks():  # type: ignore[no-untyped-def]
    from app.infrastructure.db.models.operations import DistributedLock

    return DistributedLock


# --------------------------------------------------------------------------- #
# P1 — happy path                                                             #
# --------------------------------------------------------------------------- #
async def test_p1_publish_runs_to_succeeded(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    creds = _StubCredentialStore()
    resolver = _StorageResolver(_InMemoryObjectStorage("media", b"\x01\x02\x03\x04"))
    registry = DestinationRegistry({"mock": MockDestination()})

    create = CreatePublishJob(uow, supported_platforms={"mock"})  # type: ignore[arg-type]
    created = await create.execute(
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        export_job_id=ids["export_job_id"],  # type: ignore[arg-type]
        social_account_id=ids["social_account_id"],  # type: ignore[arg-type]
    )
    assert created.created is True
    assert created.job.status == PublishStatus.QUEUED.value
    job_id = created.job.id

    worker = PublishWorker(
        uow,  # type: ignore[arg-type]
        ProcessPublishJob(uow, resolver, creds, registry),  # type: ignore[arg-type]
    )
    poll = await worker.run_once()
    assert poll.scanned == 1
    assert [o.status for o in poll.outcomes] == ["published"]

    settled = await uow.publish_jobs.get_owned(
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        publish_job_id=job_id,
    )
    assert settled is not None
    assert settled.status == PublishStatus.SUCCEEDED.value
    assert settled.attempt == 1
    assert settled.platform_post_id is not None
    assert settled.platform_post_url is not None
    assert settled.published_at is not None
    assert settled.error is None

    # Credential-blind: the worker asked the service for a bearer; nothing else touched creds.
    assert creds.authorized == [ids["social_account_id"]]

    # Transactional outbox: created → succeeded (DQ4/DQ7 — terminal events only).
    assert await _outbox_events(session, job_id) == ["PublishJobCreated", "PublishJobSucceeded"]

    # Both leases were released cleanly (no dangling locks).
    assert await _lock_count(session) == 0


# --------------------------------------------------------------------------- #
# P2 — idempotent replay                                                      #
# --------------------------------------------------------------------------- #
async def test_p2_repeat_create_is_idempotent(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    create = CreatePublishJob(uow, supported_platforms={"mock"})  # type: ignore[arg-type]

    first = await create.execute(
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        export_job_id=ids["export_job_id"],  # type: ignore[arg-type]
        social_account_id=ids["social_account_id"],  # type: ignore[arg-type]
    )
    second = await create.execute(
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        export_job_id=ids["export_job_id"],  # type: ignore[arg-type]
        social_account_id=ids["social_account_id"],  # type: ignore[arg-type]
    )
    assert first.created is True
    assert second.created is False
    assert second.job.id == first.job.id

    # Exactly one job for the caller; a single create event.
    jobs = await uow.publish_jobs.list_for_owner(
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
    )
    assert len(jobs) == 1
    assert await _outbox_events(session, first.job.id) == ["PublishJobCreated"]


# --------------------------------------------------------------------------- #
# P3 — permanent destination failure                                          #
# --------------------------------------------------------------------------- #
async def test_p3_permanent_failure_is_terminal(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    creds = _StubCredentialStore()
    resolver = _StorageResolver(_InMemoryObjectStorage("media", b"\x01\x02\x03\x04"))
    registry = DestinationRegistry({"mock": _RejectingDestination()})

    create = CreatePublishJob(uow, supported_platforms={"mock"})  # type: ignore[arg-type]
    created = await create.execute(
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        export_job_id=ids["export_job_id"],  # type: ignore[arg-type]
        social_account_id=ids["social_account_id"],  # type: ignore[arg-type]
    )
    job_id = created.job.id

    worker = PublishWorker(
        uow,  # type: ignore[arg-type]
        ProcessPublishJob(uow, resolver, creds, registry),  # type: ignore[arg-type]
    )
    poll = await worker.run_once()
    assert [o.status for o in poll.outcomes] == ["failed"]

    settled = await uow.publish_jobs.get_owned(
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        publish_job_id=job_id,
    )
    assert settled is not None
    assert settled.status == PublishStatus.FAILED.value
    assert settled.attempt == 1  # permanent → no retry
    assert settled.error is not None and settled.error["code"] == "rejected"
    assert settled.finished_at is not None

    assert await _outbox_events(session, job_id) == ["PublishJobCreated", "PublishJobFailed"]
    assert await _lock_count(session) == 0


# --------------------------------------------------------------------------- #
# P4 — creator scheduling (α8.9b): publish_at persists + runtime unchanged     #
# --------------------------------------------------------------------------- #
async def test_p4_scheduled_publish_persists_publish_at_and_still_runs(
    session: AsyncSession,
) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    creds = _StubCredentialStore()
    resolver = _StorageResolver(_InMemoryObjectStorage("media", b"\x01\x02\x03\x04"))
    registry = DestinationRegistry({"mock": MockDestination()})

    when = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
    create = CreatePublishJob(uow, supported_platforms={"mock"})  # type: ignore[arg-type]
    created = await create.execute(
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        export_job_id=ids["export_job_id"],  # type: ignore[arg-type]
        social_account_id=ids["social_account_id"],  # type: ignore[arg-type]
        publish_at=when,
    )
    job_id = created.job.id
    # SC1 — platform-native schedule lives in the content package; worker deferral untouched.
    assert created.job.content_package.publish_at == when
    assert created.job.scheduled_at is None

    # Round-trips through the publish_jobs.content_package JSONB (fresh read via the repo).
    reloaded = await uow.publish_jobs.get_owned(
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        publish_job_id=job_id,
    )
    assert reloaded is not None
    assert reloaded.content_package.publish_at == when

    # Preserve current runtime behaviour: the scheduled job still uploads on the next pass.
    worker = PublishWorker(
        uow,  # type: ignore[arg-type]
        ProcessPublishJob(uow, resolver, creds, registry),  # type: ignore[arg-type]
    )
    poll = await worker.run_once()
    assert [o.status for o in poll.outcomes] == ["published"]

    settled = await uow.publish_jobs.get_owned(
        tenant_id=ids["tenant_id"],  # type: ignore[arg-type]
        owner_user_id=ids["user_id"],  # type: ignore[arg-type]
        publish_job_id=job_id,
    )
    assert settled is not None
    assert settled.status == PublishStatus.SUCCEEDED.value
    assert settled.content_package.publish_at == when  # schedule preserved through settlement
    assert await _lock_count(session) == 0
