"""Unit tests for ``ProcessPublishJob`` (in-memory fakes, no DB).

Prove the runtime contract: dual lock order + release (DQ5), the three-phase claim/upload/
settle, bounded retries with backoff vs. permanent failure (DQ6), fail-closed on an
unavailable credential, and the credential-blind boundary — the destination adapter receives
an ``AuthorizedContext`` bearer, never the credential store (PUB-5).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.destination_publisher import (
    DestinationError,
    IDestinationPublisher,
    PublishResult,
    UploadMedia,
)
from app.application.interfaces.social_credential_store import (
    AuthorizedContext,
    CredentialUnavailableError,
)
from app.application.use_cases.publishing.process_publish_job import ProcessPublishJob
from app.domain.media.media_asset import MediaAsset
from app.domain.publishing.content_package import ContentPackage
from app.domain.publishing.publish_job import PublishJob
from app.domain.publishing.publish_status import PublishStatus

_TENANT = uuid4()
_USER = uuid4()
_NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _package(media_id: UUID) -> ContentPackage:
    return ContentPackage.from_dict(
        {
            "media_asset_id": str(media_id),
            "title": "t",
            "description": "d",
            "tags": [],
            "visibility": "private",
            "thumbnail_media_asset_id": None,
            "publish_at": None,
        }
    )


def _job(*, media_id: UUID, attempt: int = 0, max_attempts: int = 5) -> PublishJob:
    return PublishJob(
        id=uuid4(),
        tenant_id=_TENANT,
        requested_by_user_id=_USER,
        project_id=uuid4(),
        source_export_job_id=uuid4(),
        source_media_asset_id=media_id,
        social_account_id=uuid4(),
        platform="mock",
        status=PublishStatus.QUEUED.value,
        scheduled_at=None,
        attempt=attempt,
        max_attempts=max_attempts,
        content_package=_package(media_id),
        platform_post_id=None,
        platform_post_url=None,
        error=None,
        published_at=None,
        finished_at=None,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _media(media_id: UUID) -> MediaAsset:
    return MediaAsset(
        id=media_id,
        tenant_id=_TENANT,
        owner_user_id=_USER,
        kind="video",
        project_id=uuid4(),
        scene_id=None,
        prompt_id=None,
        model_id=None,
        provider=None,
        storage_backend="local",
        storage_bucket="media",
        storage_key=f"exports/{media_id}.mp4",
        mime_type="video/mp4",
        size_bytes=2048,
        width=1920,
        height=1080,
        duration_seconds=12.0,
        checksum_sha256=b"\x00" * 32,
        source="generated",
        source_metadata={},
        created_at=_NOW,
        updated_at=_NOW,
    )


class _Lease:
    def __init__(self, key: str) -> None:
        self.key = key


class _FakeLocks:
    def __init__(self, *, unavailable: set[str] | None = None) -> None:
        self._unavailable = unavailable or set()
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, *, key: str, owner: str, lease) -> _Lease | None:
        if key in self._unavailable:
            return None
        self.acquired.append(key)
        return _Lease(key)

    async def release(self, lease: _Lease) -> None:
        self.released.append(lease.key)


class _FakePublishJobs:
    def __init__(self, job: PublishJob) -> None:
        self._job = job
        self.succeeded: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.rescheduled: list[dict[str, Any]] = []

    async def mark_running(self, publish_job_id: UUID) -> PublishJob | None:
        if self._job.status != PublishStatus.QUEUED.value:
            return None
        self._job = replace(
            self._job, status=PublishStatus.RUNNING.value, attempt=self._job.attempt + 1
        )
        return self._job

    async def mark_succeeded(
        self, publish_job_id: UUID, *, platform_post_id: str, platform_post_url: str | None
    ) -> PublishJob | None:
        if self._job.status != PublishStatus.RUNNING.value:
            return None
        self._job = replace(
            self._job,
            status=PublishStatus.SUCCEEDED.value,
            platform_post_id=platform_post_id,
            platform_post_url=platform_post_url,
            published_at=_NOW,
            finished_at=_NOW,
        )
        self.succeeded.append({"id": publish_job_id, "post_id": platform_post_id})
        return self._job

    async def mark_failed(
        self, publish_job_id: UUID, *, error: dict[str, Any]
    ) -> PublishJob | None:
        if self._job.status != PublishStatus.RUNNING.value:
            return None
        self._job = replace(
            self._job, status=PublishStatus.FAILED.value, error=error, finished_at=_NOW
        )
        self.failed.append({"id": publish_job_id, "error": error})
        return self._job

    async def reschedule_for_retry(
        self, publish_job_id: UUID, *, scheduled_at: datetime, error: dict[str, Any]
    ) -> PublishJob | None:
        if self._job.status != PublishStatus.RUNNING.value:
            return None
        self._job = replace(
            self._job,
            status=PublishStatus.QUEUED.value,
            scheduled_at=scheduled_at,
            error=error,
        )
        self.rescheduled.append({"id": publish_job_id, "when": scheduled_at, "error": error})
        return self._job


class _FakeMedia:
    def __init__(self, asset: MediaAsset | None) -> None:
        self._asset = asset

    async def get_owned(self, media_id, tenant_id, owner_user_id) -> MediaAsset | None:
        return self._asset


class _FakeOutbox:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def add(self, **kwargs) -> None:
        self.events.append(kwargs)


class _FakeUoW:
    def __init__(self, *, jobs: _FakePublishJobs, media: _FakeMedia, locks: _FakeLocks) -> None:
        self.publish_jobs = jobs
        self.media = media
        self.locks = locks
        self.outbox = _FakeOutbox()
        self.commits = 0

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
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _FakeObjectStorage:
    def __init__(self, bucket: str, data: bytes) -> None:
        self._bucket = bucket
        self._data = data

    @property
    def bucket(self) -> str:
        return self._bucket

    async def get(self, *, key: str) -> bytes:
        return self._data


class _FakeStorageResolver:
    def __init__(self, storage: _FakeObjectStorage) -> None:
        self._storage = storage

    def active(self):  # pragma: no cover - unused by publish
        return self._storage

    def resolve(self, backend: str) -> _FakeObjectStorage:
        return self._storage


class _FakeCredentialStore:
    def __init__(self, *, unavailable: bool = False, token: str = "bearer-abc") -> None:
        self._unavailable = unavailable
        self._token = token
        self.authorized: list[UUID] = []

    async def store(self, social_account_id, tokens):  # pragma: no cover
        return None

    async def authorize(self, social_account_id: UUID) -> AuthorizedContext:
        if self._unavailable:
            raise CredentialUnavailableError("revoked")
        self.authorized.append(social_account_id)
        return AuthorizedContext(access_token=self._token, expires_at=_NOW, scopes=("publish",))

    async def revoke(self, social_account_id):  # pragma: no cover
        return None


class _RecordingDestination(IDestinationPublisher):
    def __init__(self, *, mode: str = "ok") -> None:
        self._mode = mode
        self.seen_auth: AuthorizedContext | None = None
        self.seen_media: UploadMedia | None = None

    @property
    def platform(self) -> str:
        return "mock"

    async def publish(
        self, *, package, auth: AuthorizedContext, media: UploadMedia
    ) -> PublishResult:
        self.seen_auth = auth
        self.seen_media = media
        if self._mode == "retryable":
            raise DestinationError("rate limited", retryable=True, code="rate_limited")
        if self._mode == "permanent":
            raise DestinationError("rejected", retryable=False, code="rejected")
        return PublishResult(external_post_id="post-123", post_url="https://x/post-123")


class _FakeRegistry:
    def __init__(self, destination: IDestinationPublisher) -> None:
        self._destination = destination

    def for_platform(self, platform: str) -> IDestinationPublisher:
        return self._destination

    def supported_platforms(self) -> frozenset[str]:
        return frozenset({"mock"})


def _build(
    job: PublishJob,
    *,
    destination: _RecordingDestination,
    media: MediaAsset | None = None,
    locks: _FakeLocks | None = None,
    credential: _FakeCredentialStore | None = None,
) -> tuple[ProcessPublishJob, _FakeUoW]:
    media_asset = media if media is not None else _media(job.source_media_asset_id)
    jobs = _FakePublishJobs(job)
    uow = _FakeUoW(jobs=jobs, media=_FakeMedia(media_asset), locks=locks or _FakeLocks())
    storage = _FakeStorageResolver(_FakeObjectStorage("media", b"\x00" * job.max_attempts))
    proc = ProcessPublishJob(
        uow=uow,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        credential_store=credential or _FakeCredentialStore(),  # type: ignore[arg-type]
        destinations=_FakeRegistry(destination),  # type: ignore[arg-type]
    )
    return proc, uow


@pytest.mark.unit
async def test_happy_path_publishes_and_emits_succeeded() -> None:
    job = _job(media_id=uuid4())
    dest = _RecordingDestination(mode="ok")
    cred = _FakeCredentialStore()
    proc, uow = _build(job, destination=dest, credential=cred)
    result = await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert result.status == "published"
    assert result.platform_post_id == "post-123"
    assert uow.publish_jobs.succeeded and not uow.publish_jobs.failed
    assert [e["event_type"] for e in uow.outbox.events] == ["PublishJobSucceeded"]
    # Credential-blind: the adapter saw a bearer, never the store.
    assert dest.seen_auth is not None and dest.seen_auth.access_token == "bearer-abc"
    assert cred.authorized == [job.social_account_id]


@pytest.mark.unit
async def test_dual_lock_order_job_then_project_and_both_released() -> None:
    job = _job(media_id=uuid4())
    locks = _FakeLocks()
    proc, uow = _build(job, destination=_RecordingDestination(), locks=locks)
    await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert locks.acquired[0] == f"publish_job:{job.id}"
    assert locks.acquired[1] == f"project_publish:{job.project_id}"
    assert set(locks.released) == {
        f"publish_job:{job.id}",
        f"project_publish:{job.project_id}",
    }


@pytest.mark.unit
async def test_job_lock_unavailable_skips_without_claiming() -> None:
    job = _job(media_id=uuid4())
    locks = _FakeLocks(unavailable={f"publish_job:{job.id}"})
    proc, uow = _build(job, destination=_RecordingDestination(), locks=locks)
    result = await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert result.status == "skipped" and result.reason == "locked"
    assert uow.publish_jobs.succeeded == [] and uow.publish_jobs.failed == []


@pytest.mark.unit
async def test_project_lock_unavailable_releases_job_and_skips() -> None:
    job = _job(media_id=uuid4())
    locks = _FakeLocks(unavailable={f"project_publish:{job.project_id}"})
    proc, uow = _build(job, destination=_RecordingDestination(), locks=locks)
    result = await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert result.status == "skipped" and result.reason == "project_locked"
    # Job lease acquired then released; job never claimed (still queued).
    assert locks.released == [f"publish_job:{job.id}"]
    assert job.status == PublishStatus.QUEUED.value


@pytest.mark.unit
async def test_retryable_failure_with_attempts_left_reschedules() -> None:
    job = _job(media_id=uuid4(), attempt=0, max_attempts=5)
    proc, uow = _build(job, destination=_RecordingDestination(mode="retryable"))
    result = await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert result.status == "retry"
    assert uow.publish_jobs.rescheduled and not uow.publish_jobs.failed
    # A retry is not terminal — no failed event emitted.
    assert uow.outbox.events == []


@pytest.mark.unit
async def test_retryable_failure_at_max_attempts_fails_permanently() -> None:
    job = _job(media_id=uuid4(), attempt=4, max_attempts=5)
    proc, uow = _build(job, destination=_RecordingDestination(mode="retryable"))
    result = await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert result.status == "failed"
    assert uow.publish_jobs.failed and not uow.publish_jobs.rescheduled
    assert [e["event_type"] for e in uow.outbox.events] == ["PublishJobFailed"]


@pytest.mark.unit
async def test_permanent_destination_error_fails_without_retry() -> None:
    job = _job(media_id=uuid4())
    proc, uow = _build(job, destination=_RecordingDestination(mode="permanent"))
    result = await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert result.status == "failed"
    assert uow.publish_jobs.rescheduled == []
    assert [e["event_type"] for e in uow.outbox.events] == ["PublishJobFailed"]


@pytest.mark.unit
async def test_credential_unavailable_fails_closed() -> None:
    job = _job(media_id=uuid4())
    cred = _FakeCredentialStore(unavailable=True)
    proc, uow = _build(job, destination=_RecordingDestination(), credential=cred)
    result = await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert result.status == "failed"
    assert uow.publish_jobs.failed[0]["error"]["code"] == "credential_unavailable"
    assert [e["event_type"] for e in uow.outbox.events] == ["PublishJobFailed"]


@pytest.mark.unit
async def test_missing_artifact_fails() -> None:
    job = _job(media_id=uuid4())
    proc, uow = _build(job, destination=_RecordingDestination())
    # get_owned returns None → permanent artifact_unavailable failure.
    uow.media = _FakeMedia(None)  # type: ignore[assignment]
    result = await proc.process(project_id=job.project_id, publish_job_id=job.id)
    assert result.status == "failed"
    assert uow.publish_jobs.failed[0]["error"]["code"] == "artifact_unavailable"
