"""Integration tests for Multi-Destination Publishing (Slice α9.4).

Drives the **real** publish stack against the live database inside a SAVEPOINT that rolls back
on teardown to prove the additive fan-out end-to-end. The batch use case is pure orchestration
over the unchanged ``CreatePublishJob`` — every account it creates is an ordinary ``PublishJob``
that the existing ``PublishWorker`` drains:

* **M1 — fan-out carry-through:** one export → two connected accounts creates **two** distinct
  ``publish_jobs`` rows; a single ``run_once()`` drains **both** to ``succeeded``.
* **M2 — best-effort:** a batch mixing a valid account with an unknown (non-owned) account records
  the unknown as a per-account ``error`` while the valid account is still created + drains — one
  bad account never blocks the rest.
* **M3 — shared fail-fast:** an unknown export aborts the whole request (``404``) and creates
  **zero** jobs.

Plus HTTP-contract checks through the real ``/api/v1/publish-jobs/batch`` ingress (auth + the
additive request-shape validators: empty / over-cap / duplicate ids).
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

from app.application.interfaces.destination_publisher import IDestinationPublisher
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.application.use_cases.publishing.create_publish_job import CreatePublishJob
from app.application.use_cases.publishing.create_publish_jobs import CreatePublishJobs
from app.application.use_cases.publishing.process_publish_job import ProcessPublishJob
from app.application.use_cases.publishing.publish_worker import PublishWorker
from app.core.errors import NotFoundError
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
    def __init__(self, token: str = "bearer-batch") -> None:
        self._token = token

    async def store(self, social_account_id, tokens):  # pragma: no cover
        return None

    async def authorize(self, social_account_id: UUID) -> AuthorizedContext:
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


async def _seed(session: AsyncSession) -> dict[str, UUID]:
    tenant_id = uuid4()
    await session.execute(insert(Tenant).values(id=tenant_id, name="Multi", slug=f"mu-{tenant_id}"))
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"mu-{user_id}@example.com",
            display_name="Multi Owner",
        )
    )
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            name="Multi Video",
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
    accounts = SocialAccountRepository(session)
    account_a = await accounts.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id=f"chan-a-{uuid4()}",
        display_name="Mock Channel A",
        scopes=("publish",),
    )
    account_b = await accounts.upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="mock",
        external_account_id=f"chan-b-{uuid4()}",
        display_name="Mock Channel B",
        scopes=("publish",),
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "export_job_id": export_job_id,
        "account_a": account_a.id,
        "account_b": account_b.id,
    }


def _runtime(uow: _PublishUnitOfWork, destination: IDestinationPublisher) -> PublishWorker:
    resolver = _StorageResolver(_InMemoryObjectStorage("media", b"\x01\x02\x03\x04"))
    return PublishWorker(
        uow,  # type: ignore[arg-type]
        ProcessPublishJob(uow, resolver, _StubCredentialStore(), DestinationRegistry({"mock": destination})),  # type: ignore[arg-type]
    )


def _fan_out(uow: _PublishUnitOfWork) -> CreatePublishJobs:
    return CreatePublishJobs(create_one=CreatePublishJob(uow, supported_platforms={"mock"}))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# M1 — fan-out to two accounts → two jobs → both drain to succeeded            #
# --------------------------------------------------------------------------- #
async def test_fan_out_creates_two_jobs_and_both_publish(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)

    result = await _fan_out(uow).execute(
        owner_user_id=ids["user_id"],
        tenant_id=ids["tenant_id"],
        export_job_id=ids["export_job_id"],
        social_account_ids=[ids["account_a"], ids["account_b"]],
    )

    assert [i.social_account_id for i in result.items] == [ids["account_a"], ids["account_b"]]
    assert all(i.created and i.job is not None and i.error is None for i in result.items)
    job_ids = {i.job.id for i in result.items if i.job is not None}
    assert len(job_ids) == 2  # two distinct publish jobs

    poll = await _runtime(uow, MockDestination()).run_once()
    assert [o.status for o in poll.outcomes] == ["published", "published"]

    for item in result.items:
        assert item.job is not None
        settled = await uow.publish_jobs.get_owned(
            tenant_id=ids["tenant_id"], owner_user_id=ids["user_id"], publish_job_id=item.job.id
        )
        assert settled is not None
        assert settled.status == PublishStatus.SUCCEEDED.value


# --------------------------------------------------------------------------- #
# M2 — best-effort: a bad account is isolated; the valid one still publishes   #
# --------------------------------------------------------------------------- #
async def test_best_effort_isolates_a_bad_account(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)
    unknown_account = uuid4()  # not the caller's → per-account 404, others proceed

    result = await _fan_out(uow).execute(
        owner_user_id=ids["user_id"],
        tenant_id=ids["tenant_id"],
        export_job_id=ids["export_job_id"],
        social_account_ids=[ids["account_a"], unknown_account],
    )

    assert result.items[0].created is True and result.items[0].error is None
    assert result.items[1].social_account_id == unknown_account
    assert result.items[1].created is False
    assert result.items[1].job is None
    assert result.items[1].error is not None
    assert result.items[1].error.code == "NOT_FOUND"

    poll = await _runtime(uow, MockDestination()).run_once()
    assert [o.status for o in poll.outcomes] == ["published"]  # only the valid job exists

    assert result.items[0].job is not None
    settled = await uow.publish_jobs.get_owned(
        tenant_id=ids["tenant_id"],
        owner_user_id=ids["user_id"],
        publish_job_id=result.items[0].job.id,
    )
    assert settled is not None
    assert settled.status == PublishStatus.SUCCEEDED.value


# --------------------------------------------------------------------------- #
# M3 — shared fail-fast: unknown export aborts the whole request, zero jobs    #
# --------------------------------------------------------------------------- #
async def test_unknown_export_aborts_and_creates_no_jobs(session: AsyncSession) -> None:
    ids = await _seed(session)
    uow = _PublishUnitOfWork(session)

    with pytest.raises(NotFoundError):
        await _fan_out(uow).execute(
            owner_user_id=ids["user_id"],
            tenant_id=ids["tenant_id"],
            export_job_id=uuid4(),  # not the caller's export
            social_account_ids=[ids["account_a"], ids["account_b"]],
        )

    # No job was queued: the poller finds nothing claimable.
    poll = await _runtime(uow, MockDestination()).run_once()
    assert poll.scanned == 0


# --------------------------------------------------------------------------- #
# HTTP contract — auth + additive request-shape validators                     #
# --------------------------------------------------------------------------- #
def _bearer(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> str:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"mu-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "MU",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["access_token"]


async def test_api_batch_requires_auth(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/publish-jobs/batch",
        json={"export_job_id": str(uuid4()), "social_account_ids": [str(uuid4())]},
    )
    assert r.status_code == 401, r.text


async def test_api_batch_empty_list_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(
        "/api/v1/publish-jobs/batch",
        headers=_bearer(access),
        json={"export_job_id": str(uuid4()), "social_account_ids": []},
    )
    assert r.status_code == 422, r.text


async def test_api_batch_over_cap_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(
        "/api/v1/publish-jobs/batch",
        headers=_bearer(access),
        json={
            "export_job_id": str(uuid4()),
            "social_account_ids": [str(uuid4()) for _ in range(21)],
        },
    )
    assert r.status_code == 422, r.text


async def test_api_batch_duplicate_ids_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    dup = str(uuid4())
    r = await client.post(
        "/api/v1/publish-jobs/batch",
        headers=_bearer(access),
        json={"export_job_id": str(uuid4()), "social_account_ids": [dup, dup]},
    )
    assert r.status_code == 422, r.text
