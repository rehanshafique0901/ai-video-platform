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

from app.application.interfaces.repositories import (
    IProjectRepository,
    IRoleRepository,
    ISceneRepository,
    ISessionRepository,
    ITenantRepository,
    IUserRepository,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.infrastructure.repositories.project_repository import ProjectRepository
from app.infrastructure.repositories.role_repository import RoleRepository
from app.infrastructure.repositories.scene_repository import SceneRepository
from app.infrastructure.repositories.session_repository import SessionRepository
from app.infrastructure.repositories.tenant_repository import TenantRepository
from app.infrastructure.repositories.user_repository import UserRepository


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
