"""Integration tests for the TikTok destination inside the real publish runtime (α9.6).

Drives the **real** publish stack — ``CreatePublishJob`` → ``PublishWorker`` →
``ProcessPublishJob`` → the real :class:`TikTokDestination` (network-free via ``MockTransport``)
— against the live database inside a SAVEPOINT, proving the second destination settles through
exactly the same runtime as YouTube with **no upstream TikTok-specific logic**.

Four flows:

* **T1 — happy path:** create → claim → chunked upload → poll ``PUBLISH_COMPLETE`` →
  ``succeeded``, with ``platform_post_id`` holding the durable ``publish_id`` (α9.6 ruling 3).
* **T2 — retryable pre-upload failure:** nothing was transmitted, so the job is requeued with
  backoff and **no** ``PublishJobFailed`` is emitted.
* **T3 — PUB-11 indeterminate outcome:** the poll budget expires after bytes were accepted, so
  the job settles **failed** and is **never retried** (no duplicate-post risk).
* **T4 — refresh-token rotation:** TikTok rotates the refresh token; the rotated value must be
  re-encrypted into the stored credential while replay (``authorize``) keeps working.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.clock import IClock
from app.application.interfaces.social_credential_store import (
    AuthorizedContext,
    GrantedTokens,
)
from app.application.use_cases.publishing.create_publish_job import CreatePublishJob
from app.application.use_cases.publishing.process_publish_job import ProcessPublishJob
from app.application.use_cases.publishing.publish_worker import PublishWorker
from app.domain.export.export_status import ExportStatus
from app.domain.publishing.publish_status import PublishStatus
from app.domain.publishing.social_account import AccountStatus
from app.domain.render.render_status import RenderStatus
from app.infrastructure.db.models.events import EventOutbox
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.jobs import ExportJob as ExportJobRow, RenderJob as RenderJobRow
from app.infrastructure.db.models.media import MediaAsset as MediaAssetRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.publishing import (
    SocialAccount as SocialAccountRow,
    SocialCredential as SocialCredentialRow,
)
from app.infrastructure.db.models.timeline import Timeline as TimelineRow
from app.infrastructure.publishing.credentials.credential_service import SocialCredentialService
from app.infrastructure.publishing.credentials.envelope import EncryptedBlob, EnvelopeCipher
from app.infrastructure.publishing.credentials.master_key import EnvMasterKeyProvider
from app.infrastructure.publishing.destinations.registry import DestinationRegistry
from app.infrastructure.publishing.destinations.tiktok import TikTokDestination
from app.infrastructure.publishing.oauth.tiktok_oauth_client import TikTokOAuthClient
from app.infrastructure.repositories.distributed_lock_manager import (
    SqlAlchemyDistributedLockManager,
)
from app.infrastructure.repositories.event_outbox_repository import EventOutboxRepository
from app.infrastructure.repositories.media_repository import MediaRepository
from app.infrastructure.repositories.project_repository import ProjectRepository
from app.infrastructure.repositories.publish_job_repository import PublishJobRepository
from app.infrastructure.repositories.social_account_repository import SocialAccountRepository

pytestmark = pytest.mark.integration

_API = "https://open.tiktokapis.com"
_CREATOR_PATH = "/v2/post/publish/creator_info/query/"
_INIT_PATH = "/v2/post/publish/video/init/"
_STATUS_PATH = "/v2/post/publish/status/fetch/"
_TOKEN_URL = f"{_API}/v2/oauth/token/"
_UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=1&upload_token=t"
_PUBLISH_ID = "v_pub_file~v2-1.987654321"
_OK = {"code": "ok", "message": ""}
_ARTIFACT = b"\x01\x02\x03\x04" * 512  # 2048 bytes


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
    """Hands the runtime a ready bearer — the adapter stays credential-blind (PUB-5)."""

    async def store(self, social_account_id, tokens):  # pragma: no cover
        return None

    async def authorize(self, social_account_id: UUID) -> AuthorizedContext:
        return AuthorizedContext(
            access_token="act.integration", expires_at=datetime.now(UTC), scopes=("video.publish",)
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


class _StubClock(IClock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _tiktok(handler, *, budget: float = 30.0) -> TikTokDestination:  # type: ignore[no-untyped-def]
    return TikTokDestination(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_base_url=_API,
        chunk_size_bytes=5 * 1024 * 1024,
        status_poll_interval_seconds=0.0,
        status_poll_budget_seconds=budget,
    )


def _flow(*, init_error: str | None = None, statuses: list[dict[str, object]] | None = None):
    """The TikTok wire protocol, with the init and status phases overridable."""
    remaining = list(statuses if statuses is not None else [{"status": "PUBLISH_COMPLETE"}])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == _CREATOR_PATH:
            return httpx.Response(
                200,
                json={
                    "data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]},
                    "error": _OK,
                },
            )
        if request.method == "POST" and path == _INIT_PATH:
            if init_error is not None:
                return httpx.Response(403, json={"data": {}, "error": {"code": init_error}})
            return httpx.Response(
                200,
                json={"data": {"publish_id": _PUBLISH_ID, "upload_url": _UPLOAD_URL}, "error": _OK},
            )
        if request.method == "PUT":
            return httpx.Response(201)
        if request.method == "POST" and path == _STATUS_PATH:
            data = remaining[0] if len(remaining) == 1 else remaining.pop(0)
            return httpx.Response(200, json={"data": data, "error": _OK})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    return handler


# --------------------------------------------------------------------------- #
# Seed                                                                        #
# --------------------------------------------------------------------------- #
async def _seed(session: AsyncSession) -> dict[str, UUID]:
    tenant_id, user_id, project_id, timeline_id = uuid4(), uuid4(), uuid4(), uuid4()
    await session.execute(insert(Tenant).values(id=tenant_id, name="TT", slug=f"tt-{tenant_id}"))
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"tt-{user_id}@example.com",
            display_name="TikTok Owner",
        )
    )
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            name="Short Clip",
            aspect_ratio="vertical",
        )
    )
    await session.execute(
        insert(TimelineRow).values(
            id=timeline_id,
            project_id=project_id,
            aspect_ratio="9:16",
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
                size_bytes=len(_ARTIFACT),
                checksum_sha256=b"\x00" * 32,
                source="generated",
            )
        )
    render_job_id, export_job_id = uuid4(), uuid4()
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
    await session.execute(
        insert(ExportJobRow).values(
            id=export_job_id,
            render_job_id=render_job_id,
            requested_by_user_id=user_id,
            format="mp4",
            quality="hd_1080p",
            orientation="vertical",
            status=ExportStatus.SUCCEEDED.value,
            output_media_asset_id=delivery_id,
        )
    )
    await session.flush()
    account = await SocialAccountRepository(session).upsert_connected(
        tenant_id=tenant_id,
        user_id=user_id,
        platform="tiktok",
        external_account_id=f"open-{uuid4()}",
        display_name="TikTok Creator",
        scopes=("video.publish",),
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "export_job_id": export_job_id,
        "social_account_id": account.id,
    }


async def _events(session: AsyncSession, publish_job_id: UUID) -> list[str]:
    stmt = (
        select(EventOutbox.event_type)
        .where(EventOutbox.aggregate_type == "publish_job")
        .where(EventOutbox.aggregate_id == publish_job_id)
        .order_by(EventOutbox.occurred_at, EventOutbox.id)
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


async def _run(session: AsyncSession, ids: dict[str, UUID], destination: TikTokDestination):
    """Create a TikTok publish job and let the real worker drive it once."""
    uow = _PublishUnitOfWork(session)
    create = CreatePublishJob(uow, supported_platforms={"tiktok"})  # type: ignore[arg-type]
    created = await create.execute(
        owner_user_id=ids["user_id"],
        tenant_id=ids["tenant_id"],
        export_job_id=ids["export_job_id"],
        social_account_id=ids["social_account_id"],
    )
    assert created.created is True
    worker = PublishWorker(
        uow,  # type: ignore[arg-type]
        ProcessPublishJob(  # type: ignore[arg-type]
            uow,
            _StorageResolver(_InMemoryObjectStorage("media", _ARTIFACT)),
            _StubCredentialStore(),
            DestinationRegistry({"tiktok": destination}),
        ),
    )
    await worker.run_once()
    return created.job.id


async def _fetch(session: AsyncSession, ids: dict[str, UUID], job_id: UUID):
    """Re-read the settled job through the owner-scoped repository accessor."""
    job = await PublishJobRepository(session).get_owned(
        tenant_id=ids["tenant_id"], owner_user_id=ids["user_id"], publish_job_id=job_id
    )
    assert job is not None
    return job


# --------------------------------------------------------------------------- #
# T1 — happy path                                                             #
# --------------------------------------------------------------------------- #
async def test_t1_tiktok_publish_runs_to_succeeded(session: AsyncSession) -> None:
    ids = await _seed(session)
    job_id = await _run(session, ids, _tiktok(_flow()))

    job = await _fetch(session, ids, job_id)
    assert job.status == PublishStatus.SUCCEEDED.value
    # α9.6 ruling 3 — the durable publish_id is the stable external identifier.
    assert job.platform_post_id == _PUBLISH_ID
    # No canonical URL is derivable without the creator's username, so none is invented.
    assert job.platform_post_url is None
    assert await _events(session, job_id) == ["PublishJobCreated", "PublishJobSucceeded"]


async def test_t1b_public_post_id_does_not_replace_the_identifier(
    session: AsyncSession,
) -> None:
    """Even when TikTok discloses a public post id, the recorded identifier stays stable."""
    ids = await _seed(session)
    job_id = await _run(
        session,
        ids,
        _tiktok(
            _flow(
                statuses=[
                    {
                        "status": "PUBLISH_COMPLETE",
                        "publicaly_available_post_id": ["7300000000000000000"],
                    }
                ]
            )
        ),
    )

    job = await _fetch(session, ids, job_id)
    assert job.platform_post_id == _PUBLISH_ID


# --------------------------------------------------------------------------- #
# T2 — retryable pre-upload failure                                           #
# --------------------------------------------------------------------------- #
async def test_t2_pre_upload_failure_requeues_without_failing(session: AsyncSession) -> None:
    ids = await _seed(session)
    job_id = await _run(session, ids, _tiktok(_flow(init_error="reached_active_user_cap")))

    job = await _fetch(session, ids, job_id)
    # Nothing was transmitted, so the runtime may safely retry with backoff.
    assert job.status == PublishStatus.QUEUED.value
    assert job.attempt == 1
    assert job.scheduled_at is not None and job.scheduled_at > datetime.now(UTC)
    assert job.error is not None and job.error["code"] == "quota_exceeded"
    assert "PublishJobFailed" not in await _events(session, job_id)


# --------------------------------------------------------------------------- #
# T3 — PUB-11 indeterminate outcome                                           #
# --------------------------------------------------------------------------- #
async def test_t3_indeterminate_outcome_fails_and_is_never_retried(
    session: AsyncSession,
) -> None:
    """The bytes were accepted but no terminal state arrived: fail, never retry (PUB-11)."""
    ids = await _seed(session)
    job_id = await _run(
        session,
        ids,
        _tiktok(_flow(statuses=[{"status": "PROCESSING_UPLOAD"}]), budget=0.0),
    )

    job = await _fetch(session, ids, job_id)
    assert job.status == PublishStatus.FAILED.value
    # Attempts remain (1 of 5) yet the job is terminal — proof the permanent classification,
    # not attempt exhaustion, stopped it. A retry would risk a duplicate public post.
    assert job.attempt == 1
    assert job.max_attempts == 5
    assert job.error is not None and job.error["code"] == "ambiguous_upload_outcome"
    assert await _events(session, job_id) == ["PublishJobCreated", "PublishJobFailed"]


# --------------------------------------------------------------------------- #
# T4 — refresh-token rotation reaches the encrypted credential                #
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def bound(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.connect() as connection:
        outer_tx = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield factory
        finally:
            await outer_tx.rollback()


async def _seed_tiktok_account(factory: async_sessionmaker[AsyncSession]) -> UUID:
    tenant_id, user_id, account_id = uuid4(), uuid4(), uuid4()
    async with factory() as session:
        await session.execute(
            insert(Tenant).values(id=tenant_id, name="TTC", slug=f"ttc-{tenant_id}")
        )
        await session.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"ttc-{user_id}@example.com",
                display_name="Rotation Owner",
            )
        )
        await session.execute(
            insert(SocialAccountRow).values(
                id=account_id,
                tenant_id=tenant_id,
                user_id=user_id,
                platform="tiktok",
                external_account_id=f"open-{account_id}",
                display_name="TikTok Creator",
                status=AccountStatus.CONNECTED.value,
                scopes=["video.publish"],
                connected_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return account_id


async def test_t4_rotated_refresh_token_is_persisted_and_replay_still_works(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """TikTok rotates the refresh token; the stored ciphertext must carry the NEW value.

    Regression guard: if the rotated token were dropped, everything would look healthy until
    the 24h access token expired and the next refresh failed with the now-invalid old token.
    """
    clock = _StubClock(datetime(2026, 7, 29, 12, 0, tzinfo=UTC))
    account_id = await _seed_tiktok_account(bound)

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "open_id": "open-abc",
                "access_token": "act.rotated",
                "refresh_token": "rft.ROTATED",
                "expires_in": 86400,
                "scope": "video.publish",
                "token_type": "Bearer",
            },
        )

    oauth = TikTokOAuthClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
        client_key="key",
        client_secret="secret",
        clock=clock,
        scopes=("video.publish",),
        authorize_url="https://www.tiktok.com/v2/auth/authorize/",
        token_url=_TOKEN_URL,
        revoke_url=f"{_API}/v2/oauth/revoke/",
        api_base_url=_API,
    )
    service = SocialCredentialService(
        session_factory=bound,
        cipher=EnvelopeCipher(EnvMasterKeyProvider(version="v1", secret="integration-master-key")),
        oauth_clients={"tiktok": oauth},
        clock=clock,
    )

    # Store a credential that is already inside the proactive-refresh window.
    await service.store(
        account_id,
        GrantedTokens(
            access_token="act.original",
            refresh_token="rft.original",
            expires_at=clock.now() + timedelta(seconds=5),
            scopes=("video.publish",),
        ),
    )

    ctx = await service.authorize(account_id)
    assert ctx.access_token == "act.rotated"

    # The ciphertext must now decrypt to the ROTATED refresh token — and still be encrypted.
    async with bound() as session:
        row = (
            await session.execute(
                select(SocialCredentialRow).where(
                    SocialCredentialRow.social_account_id == account_id
                )
            )
        ).scalar_one()
    assert b"rft.ROTATED" not in row.ciphertext
    assert b"rft.original" not in row.ciphertext
    assert row.access_token_expires_at == clock.now() + timedelta(seconds=86400)

    stored = EnvelopeCipher(
        EnvMasterKeyProvider(version="v1", secret="integration-master-key")
    ).decrypt(
        EncryptedBlob(
            ciphertext=row.ciphertext,
            nonce=row.nonce,
            wrapped_dek=row.wrapped_dek,
            key_version=row.key_version,
            algorithm=row.algorithm,
        )
    )
    assert b"rft.ROTATED" in stored
    assert b"rft.original" not in stored

    # Replay behaviour preserved: a second authorize still returns a usable bearer.
    again = await service.authorize(account_id)
    assert again.access_token == "act.rotated"
    assert again.scopes == ("video.publish",)
