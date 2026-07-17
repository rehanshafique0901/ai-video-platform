"""Port: Unit of Work — transaction boundary abstraction.

Use cases depend on this ABC so they remain free of SQLAlchemy. The
concrete implementation lives in ``app.infrastructure.uow``.

Slice α2a: the UoW exposes typed repository attributes
(``users`` / ``tenants`` / ``sessions`` / ``roles``) which are
populated by the concrete ``__aenter__`` from the same
``AsyncSession`` the UoW itself owns. Use cases therefore never see a
SQLAlchemy session; they call ``async with uow: await uow.users.add(...)``.

The attributes are declared without defaults: subclasses are
responsible for initialising them in ``__aenter__``. Access before
entering the context raises ``AttributeError`` at runtime and is
flagged by mypy as unreachable in the "with" block only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self

from app.application.interfaces.locks import IDistributedLockManager
from app.application.interfaces.repositories import (
    IEventOutboxRepository,
    IMediaRepository,
    IProjectRepository,
    IProjectVersionRepository,
    IPromptRepository,
    IRenderJobRepository,
    IRoleRepository,
    ISceneRepository,
    ISessionRepository,
    ITenantRepository,
    ITimelineRepository,
    IUserRepository,
    IWorkflowRunRepository,
)


class IUnitOfWork(ABC):
    """Async context manager that owns one transactional boundary.

    Usage::

        async with uow:
            user = await uow.users.get_by_email(email)
            ...
            await uow.commit()

    Exit without an explicit ``commit()`` (or with an exception) rolls
    back. ``__aexit__`` always closes the underlying session.
    """

    # Repository attributes — populated by the concrete UoW's
    # ``__aenter__`` from the session it owns. Types intentionally use
    # the ports so use cases never see infrastructure classes.
    users: IUserRepository
    tenants: ITenantRepository
    sessions: ISessionRepository
    roles: IRoleRepository
    projects: IProjectRepository  # α5a
    scenes: ISceneRepository  # α5c
    versions: IProjectVersionRepository  # α5d
    prompts: IPromptRepository  # α6.1
    media: IMediaRepository  # α6.2
    timeline: ITimelineRepository  # α6.3a
    render_jobs: IRenderJobRepository  # α7.1
    outbox: IEventOutboxRepository  # α7.1 (+ α7.3 relay read/mark surface)
    workflow_runs: IWorkflowRunRepository  # α7.2
    locks: IDistributedLockManager  # α7.3

    async def __aenter__(self) -> Self:
        return self

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...
