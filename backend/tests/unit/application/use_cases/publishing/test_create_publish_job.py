"""Unit tests for ``CreatePublishJob`` (in-memory fakes, no DB).

Prove orchestration + boundaries (PUB-1/PUB-2, DQ2): destination readiness, export
source resolution/readiness, unsupported platform, idempotent replay, and that a
``PublishJobCreated`` event is emitted on a fresh create.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace, TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.publishing.create_publish_job import CreatePublishJob
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.projects.project import Project
from app.domain.publishing.content_package import ContentPackage
from app.domain.publishing.publish_job import PublishJob, PublishSource
from app.domain.publishing.publish_status import PublishStatus
from app.domain.publishing.social_account import AccountStatus, SocialAccount

_TENANT = uuid4()
_USER = uuid4()
_NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _account(
    account_id: UUID, *, platform: str = "mock", status: AccountStatus = AccountStatus.CONNECTED
) -> SocialAccount:
    return SocialAccount(
        id=account_id,
        tenant_id=_TENANT,
        user_id=_USER,
        platform=platform,
        external_account_id=f"ext-{account_id}",
        display_name="Mock Channel",
        status=status,
        scopes=("publish",),
        connected_at=_NOW,
        revoked_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _project(project_id: UUID) -> Project:
    return Project(
        id=project_id,
        tenant_id=_TENANT,
        owner_user_id=_USER,
        folder_id=None,
        current_version_id=None,
        name="My Project",
        description=None,
        aspect_ratio="16:9",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=_NOW,
        updated_at=_NOW,
        version=1,
    )


class _FakeSocialAccounts:
    def __init__(self, account: SocialAccount | None) -> None:
        self._account = account

    async def get_owned(self, *, tenant_id, user_id, social_account_id) -> SocialAccount | None:
        if self._account is None:
            return None
        if (
            self._account.id != social_account_id
            or self._account.tenant_id != tenant_id
            or self._account.user_id != user_id
        ):
            return None
        return self._account


class _FakePublishJobs:
    def __init__(
        self,
        *,
        source: PublishSource | None,
        active: PublishJob | None = None,
        conflict: bool = False,
    ) -> None:
        self._source = source
        self._active = active
        self._conflict = conflict
        self.added: list[dict[str, Any]] = []

    async def resolve_source(
        self, export_job_id, *, tenant_id, owner_user_id
    ) -> PublishSource | None:
        return self._source

    async def get_active(self, *, source_media_asset_id, social_account_id) -> PublishJob | None:
        return self._active

    async def add(self, **kwargs) -> PublishJob:
        if self._conflict and self._active is None:
            raise ConflictError("dup")
        if self._conflict:
            raise ConflictError("dup")
        self.added.append(kwargs)
        return _job_from_add(kwargs)


class _FakeProjects:
    def __init__(self, project: Project | None) -> None:
        self._project = project

    async def get_owned(self, *, project_id, tenant_id, owner_user_id) -> Project | None:
        return self._project


class _FakeMedia:
    """Minimal owner-scoped media read for the α9.3 thumbnail validation path."""

    def __init__(self, asset: Any | None) -> None:
        self._asset = asset
        self.calls: list[tuple[Any, Any, Any]] = []

    async def get_owned(self, media_asset_id, tenant_id, owner_user_id) -> Any | None:
        self.calls.append((media_asset_id, tenant_id, owner_user_id))
        return self._asset


def _image_asset(asset_id: UUID, *, kind: str = "image") -> SimpleNamespace:
    return SimpleNamespace(id=asset_id, kind=kind, mime_type="image/jpeg", size_bytes=2048)


class _FakeOutbox:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def add(self, **kwargs) -> None:
        self.events.append(kwargs)


class _FakeUoW:
    def __init__(
        self,
        *,
        account: SocialAccount | None,
        source: PublishSource | None,
        active: PublishJob | None = None,
        project: Project | None = None,
        conflict: bool = False,
        thumbnail_asset: Any | None = None,
    ) -> None:
        self.social_accounts = _FakeSocialAccounts(account)
        self.publish_jobs = _FakePublishJobs(source=source, active=active, conflict=conflict)
        self.projects = _FakeProjects(project)
        self.media = _FakeMedia(thumbnail_asset)
        self.outbox = _FakeOutbox()
        self.committed = False

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
        self.committed = True


def _job_from_add(kwargs: dict[str, Any]) -> PublishJob:
    package = kwargs["content_package"]
    assert isinstance(package, ContentPackage)
    return PublishJob(
        id=uuid4(),
        tenant_id=kwargs["tenant_id"],
        requested_by_user_id=kwargs["requested_by_user_id"],
        project_id=kwargs["project_id"],
        source_export_job_id=kwargs["source_export_job_id"],
        source_media_asset_id=kwargs["source_media_asset_id"],
        social_account_id=kwargs["social_account_id"],
        platform=kwargs["platform"],
        status=kwargs["status"],
        scheduled_at=kwargs["scheduled_at"],
        attempt=0,
        max_attempts=kwargs["max_attempts"],
        content_package=package,
        platform_post_id=None,
        platform_post_url=None,
        error=None,
        published_at=None,
        finished_at=None,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _existing_job(source_media_asset_id: UUID, social_account_id: UUID) -> PublishJob:
    return PublishJob(
        id=uuid4(),
        tenant_id=_TENANT,
        requested_by_user_id=_USER,
        project_id=uuid4(),
        source_export_job_id=uuid4(),
        source_media_asset_id=source_media_asset_id,
        social_account_id=social_account_id,
        platform="mock",
        status=PublishStatus.QUEUED.value,
        scheduled_at=None,
        attempt=0,
        max_attempts=5,
        content_package=ContentPackage.from_dict(
            {
                "media_asset_id": str(source_media_asset_id),
                "title": "t",
                "description": "d",
                "tags": [],
                "visibility": "private",
                "thumbnail_media_asset_id": None,
                "publish_at": None,
            }
        ),
        platform_post_id=None,
        platform_post_url=None,
        error=None,
        published_at=None,
        finished_at=None,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _source(project_id: UUID, media_id: UUID, *, status: str = "succeeded") -> PublishSource:
    return PublishSource(
        export_job_id=uuid4(),
        project_id=project_id,
        source_media_asset_id=media_id,
        export_status=status,
    )


async def _run(uow: _FakeUoW, **overrides):
    use_case = CreatePublishJob(uow=uow, supported_platforms={"mock"})  # type: ignore[arg-type]
    params: dict[str, Any] = {
        "owner_user_id": _USER,
        "tenant_id": _TENANT,
        "export_job_id": uuid4(),
        "social_account_id": overrides.get("social_account_id", uuid4()),
    }
    params.update({k: v for k, v in overrides.items() if k != "social_account_id"})
    return await use_case.execute(**params)


@pytest.mark.unit
async def test_happy_path_creates_and_emits_event() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        project=_project(project_id),
    )
    result = await _run(uow, social_account_id=account_id)
    assert result.created is True
    assert result.job.platform == "mock"
    assert result.job.project_id == project_id
    assert result.job.source_media_asset_id == media_id
    assert result.job.content_package.title == "My Project"
    assert uow.committed is True
    assert len(uow.outbox.events) == 1
    assert uow.outbox.events[0]["event_type"] == "PublishJobCreated"


@pytest.mark.unit
async def test_missing_account_is_404() -> None:
    uow = _FakeUoW(account=None, source=_source(uuid4(), uuid4()))
    with pytest.raises(NotFoundError):
        await _run(uow)


@pytest.mark.unit
async def test_disconnected_account_is_422() -> None:
    account_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id, status=AccountStatus.REVOKED),
        source=_source(uuid4(), uuid4()),
    )
    with pytest.raises(ValidationFailedError):
        await _run(uow, social_account_id=account_id)


@pytest.mark.unit
async def test_unsupported_platform_is_422() -> None:
    account_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id, platform="tiktok"),
        source=_source(uuid4(), uuid4()),
    )
    with pytest.raises(ValidationFailedError):
        await _run(uow, social_account_id=account_id)


@pytest.mark.unit
async def test_missing_export_is_404() -> None:
    account_id = uuid4()
    uow = _FakeUoW(account=_account(account_id), source=None)
    with pytest.raises(NotFoundError):
        await _run(uow, social_account_id=account_id)


@pytest.mark.unit
async def test_export_not_succeeded_is_422() -> None:
    account_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(uuid4(), uuid4(), status="running"),
    )
    with pytest.raises(ValidationFailedError):
        await _run(uow, social_account_id=account_id)


@pytest.mark.unit
async def test_idempotent_replay_returns_existing() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    existing = _existing_job(media_id, account_id)
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        active=existing,
        project=_project(project_id),
    )
    result = await _run(uow, social_account_id=account_id)
    assert result.created is False
    assert result.job.id == existing.id
    assert uow.outbox.events == []


@pytest.mark.unit
async def test_create_race_conflict_recovers_to_winner() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    winner = _existing_job(media_id, account_id)
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        project=_project(project_id),
        conflict=True,
    )
    # No active on pre-check, but add() raises ConflictError and get_active then returns winner.
    uow.publish_jobs._active = None

    async def _get_active(*, source_media_asset_id, social_account_id):
        return winner

    uow.publish_jobs.get_active = _get_active  # type: ignore[assignment]
    result = await _run(uow, social_account_id=account_id)
    assert result.created is False
    assert result.job.id == winner.id


# ---- α8.9b — Creator Scheduling (publish_at threading) --------------------


@pytest.mark.unit
async def test_absent_publish_at_leaves_content_package_unscheduled() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        project=_project(project_id),
    )
    result = await _run(uow, social_account_id=account_id)
    assert result.job.content_package.publish_at is None


@pytest.mark.unit
async def test_publish_at_is_threaded_into_content_package() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    when = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        project=_project(project_id),
    )
    result = await _run(uow, social_account_id=account_id, publish_at=when)
    # Threaded into the built package + the persisted add kwargs (scheduled_at untouched).
    assert result.job.content_package.publish_at == when
    (added,) = uow.publish_jobs.added
    assert added["content_package"].publish_at == when
    assert added["scheduled_at"] is None  # SC1 — worker-side deferral is NOT used


@pytest.mark.unit
async def test_idempotent_replay_ignores_new_publish_at() -> None:
    # SC5 — a replay returns the existing job unchanged; the replay's publish_at is not applied.
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    existing = _existing_job(media_id, account_id)  # its package.publish_at is None
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        active=existing,
        project=_project(project_id),
    )
    result = await _run(
        uow, social_account_id=account_id, publish_at=datetime(2026, 8, 1, 9, 30, tzinfo=UTC)
    )
    assert result.created is False
    assert result.job.id == existing.id
    assert result.job.content_package.publish_at is None  # unchanged by the replay
    assert uow.publish_jobs.added == []


# ---- α9.3 — Publish Thumbnail Support (thumbnail_media_asset_id) -----------


@pytest.mark.unit
async def test_absent_thumbnail_leaves_content_package_thumbnail_none() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        project=_project(project_id),
    )
    result = await _run(uow, social_account_id=account_id)
    assert result.job.content_package.thumbnail_media_asset_id is None
    assert uow.media.calls == []  # no media read when no thumbnail requested


@pytest.mark.unit
async def test_owned_image_thumbnail_is_captured_into_content_package() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    thumb_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        project=_project(project_id),
        thumbnail_asset=_image_asset(thumb_id),
    )
    result = await _run(uow, social_account_id=account_id, thumbnail_media_asset_id=thumb_id)
    assert result.job.content_package.thumbnail_media_asset_id == thumb_id
    (added,) = uow.publish_jobs.added
    assert added["content_package"].thumbnail_media_asset_id == thumb_id
    # Owner-scoped lookup used the caller's tenant/user.
    assert uow.media.calls == [(thumb_id, _TENANT, _USER)]


@pytest.mark.unit
async def test_non_owned_thumbnail_is_404() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        project=_project(project_id),
        thumbnail_asset=None,  # not the caller's / missing
    )
    with pytest.raises(NotFoundError):
        await _run(uow, social_account_id=account_id, thumbnail_media_asset_id=uuid4())


@pytest.mark.unit
async def test_non_image_thumbnail_is_422() -> None:
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    thumb_id = uuid4()
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        project=_project(project_id),
        thumbnail_asset=_image_asset(thumb_id, kind="video"),
    )
    with pytest.raises(ValidationFailedError):
        await _run(uow, social_account_id=account_id, thumbnail_media_asset_id=thumb_id)


@pytest.mark.unit
async def test_idempotent_replay_ignores_new_thumbnail() -> None:
    # A replay returns the existing job unchanged; the replay's thumbnail is not applied and the
    # media repo is never consulted (the pre-check returns before validation).
    account_id = uuid4()
    project_id = uuid4()
    media_id = uuid4()
    existing = _existing_job(media_id, account_id)  # its package.thumbnail_media_asset_id is None
    uow = _FakeUoW(
        account=_account(account_id),
        source=_source(project_id, media_id),
        active=existing,
        project=_project(project_id),
        thumbnail_asset=_image_asset(uuid4()),
    )
    result = await _run(uow, social_account_id=account_id, thumbnail_media_asset_id=uuid4())
    assert result.created is False
    assert result.job.content_package.thumbnail_media_asset_id is None
    assert uow.media.calls == []
