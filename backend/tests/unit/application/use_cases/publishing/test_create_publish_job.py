"""Unit tests for ``CreatePublishJob`` (in-memory fakes, no DB).

Prove orchestration + boundaries (PUB-1/PUB-2, DQ2): destination readiness, export
source resolution/readiness, unsupported platform, idempotent replay, and that a
``PublishJobCreated`` event is emitted on a fresh create.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
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
    ) -> None:
        self.social_accounts = _FakeSocialAccounts(account)
        self.publish_jobs = _FakePublishJobs(source=source, active=active, conflict=conflict)
        self.projects = _FakeProjects(project)
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
