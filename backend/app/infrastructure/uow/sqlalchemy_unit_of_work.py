"""SQLAlchemy implementation of the UnitOfWork port.

Wraps one ``AsyncSession`` lifetime so use cases never see SQLAlchemy
directly. Exit without an explicit commit rolls back automatically;
``__aexit__`` always closes the session.

Slice α2a extends ``__aenter__`` to populate the four repository
attributes declared on ``IUnitOfWork`` — ``users``, ``tenants``,
``sessions``, ``roles`` — so use cases can call e.g.
``await uow.users.add(entity)`` without ever knowing the concrete
repository classes exist.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.locks import IDistributedLockManager
from app.application.interfaces.repositories import (
    IAnalyticsRepository,
    IEventOutboxRepository,
    IExportJobRepository,
    ILibraryRepository,
    IMediaRepository,
    IModelPricingRepository,
    INotificationRepository,
    IProjectRepository,
    IProjectVersionRepository,
    IPromptRepository,
    IProviderSettingsRepository,
    IPublishJobRepository,
    IRenderJobRepository,
    IRoleRepository,
    ISceneRepository,
    ISessionRepository,
    ISocialAccountRepository,
    ITenantRepository,
    ITimelineRepository,
    IUsageRecordRepository,
    IUserRepository,
    IWorkflowRunRepository,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.infrastructure.repositories.analytics_repository import AnalyticsRepository
from app.infrastructure.repositories.distributed_lock_manager import (
    SqlAlchemyDistributedLockManager,
)
from app.infrastructure.repositories.event_outbox_repository import EventOutboxRepository
from app.infrastructure.repositories.export_job_repository import ExportJobRepository
from app.infrastructure.repositories.library_repository import LibraryRepository
from app.infrastructure.repositories.media_repository import MediaRepository
from app.infrastructure.repositories.model_pricing_repository import ModelPricingRepository
from app.infrastructure.repositories.notification_repository import NotificationRepository
from app.infrastructure.repositories.project_repository import ProjectRepository
from app.infrastructure.repositories.project_version_repository import (
    ProjectVersionRepository,
)
from app.infrastructure.repositories.prompt_repository import PromptRepository
from app.infrastructure.repositories.provider_settings_repository import (
    ProviderSettingsRepository,
)
from app.infrastructure.repositories.publish_job_repository import PublishJobRepository
from app.infrastructure.repositories.render_job_repository import RenderJobRepository
from app.infrastructure.repositories.role_repository import RoleRepository
from app.infrastructure.repositories.scene_repository import SceneRepository
from app.infrastructure.repositories.session_repository import SessionRepository
from app.infrastructure.repositories.social_account_repository import SocialAccountRepository
from app.infrastructure.repositories.tenant_repository import TenantRepository
from app.infrastructure.repositories.timeline_repository import TimelineRepository
from app.infrastructure.repositories.usage_record_repository import UsageRecordRepository
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.repositories.workflow_run_repository import WorkflowRunRepository


class SqlAlchemyUnitOfWork(IUnitOfWork):
    """Owns one ``AsyncSession`` for the duration of an ``async with`` block."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active — use 'async with uow:'")
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self._committed = False
        # Populate the repository attributes declared on IUnitOfWork.
        # The ``cast`` calls tell mypy the concrete impls satisfy the ABCs
        # (which they do by inheritance) — the runtime type is the
        # concrete class, but callers see only the port surface.
        self.users = cast(IUserRepository, UserRepository(self._session))
        self.tenants = cast(ITenantRepository, TenantRepository(self._session))
        self.sessions = cast(ISessionRepository, SessionRepository(self._session))
        self.roles = cast(IRoleRepository, RoleRepository(self._session))
        self.projects = cast(IProjectRepository, ProjectRepository(self._session))
        self.scenes = cast(ISceneRepository, SceneRepository(self._session))
        self.versions = cast(IProjectVersionRepository, ProjectVersionRepository(self._session))
        self.prompts = cast(IPromptRepository, PromptRepository(self._session))
        self.media = cast(IMediaRepository, MediaRepository(self._session))
        self.library = cast(ILibraryRepository, LibraryRepository(self._session))
        self.timeline = cast(ITimelineRepository, TimelineRepository(self._session))
        self.render_jobs = cast(IRenderJobRepository, RenderJobRepository(self._session))
        self.export_jobs = cast(IExportJobRepository, ExportJobRepository(self._session))
        self.notifications = cast(INotificationRepository, NotificationRepository(self._session))
        self.social_accounts = cast(
            ISocialAccountRepository, SocialAccountRepository(self._session)
        )
        self.publish_jobs = cast(IPublishJobRepository, PublishJobRepository(self._session))
        self.outbox = cast(IEventOutboxRepository, EventOutboxRepository(self._session))
        self.workflow_runs = cast(IWorkflowRunRepository, WorkflowRunRepository(self._session))
        self.locks = cast(IDistributedLockManager, SqlAlchemyDistributedLockManager(self._session))
        self.provider_settings = cast(
            IProviderSettingsRepository, ProviderSettingsRepository(self._session)
        )
        self.usage = cast(IUsageRecordRepository, UsageRecordRepository(self._session))
        self.model_pricing = cast(IModelPricingRepository, ModelPricingRepository(self._session))
        self.analytics = cast(IAnalyticsRepository, AnalyticsRepository(self._session))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc is not None or not self._committed:
                await self.rollback()
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def commit(self) -> None:
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self._session is not None:
            await self._session.rollback()
