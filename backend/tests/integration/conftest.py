"""Async fixtures for integration tests.

Each test runs inside a SAVEPOINT that rolls back on teardown, so the
shared Supabase instance is never mutated by the suite. All fixtures
are function-scoped so pytest-asyncio's default function-scoped event
loop is sufficient — avoids the scope-mismatch footguns that
session-scoped async fixtures introduce. The per-test engine cost
(~1s) is acceptable for the ~8-test α1 suite.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core import container
from app.core.config import Settings
from app.infrastructure.db.session import make_engine, make_session_factory

# Windows: switch to SelectorEventLoop policy before pytest-asyncio
# creates the first event loop (it creates loops lazily on first async
# fixture/test, after this module has finished loading). psycopg's
# async driver does not support the default ProactorEventLoop on
# Windows. This block is a no-op on Linux/macOS.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _ensure_test_env_defaults() -> None:
    """Populate env vars that ``.env.validation`` may not yet carry.

    ``.env.validation`` predates Phase 3 and may lack ``JWT_SECRET`` /
    ``ENVIRONMENT``. We fill in safe test defaults if absent so the
    integration suite can run before the user updates the file.
    """
    if not os.environ.get("JWT_SECRET"):
        os.environ["JWT_SECRET"] = "test-secret-do-not-use-in-production-32chars"
    if not os.environ.get("ENVIRONMENT"):
        os.environ["ENVIRONMENT"] = "local"


@pytest.fixture
def settings() -> Settings:
    _ensure_test_env_defaults()
    return Settings()  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(settings.database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(engine)


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test session wrapped in a SAVEPOINT that rolls back on teardown.

    Classic SQLAlchemy 'join an external transaction' pattern: the
    connection holds a top-level transaction, the session begins a
    nested SAVEPOINT, and on exit the connection rolls back so nothing
    persists in the shared database.
    """
    async with engine.connect() as connection:
        outer_tx = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session_:
            await session_.begin_nested()
            try:
                yield session_
            finally:
                await session_.close()
        await outer_tx.rollback()


@pytest_asyncio.fixture
async def client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """httpx ``AsyncClient`` wired to the FastAPI app for in-process testing.

    Slice α2a: mutation handlers (``/api/v1/auth/register`` etc.) are
    now under test, so this fixture routes the container-owned
    ``get_session`` / ``get_unit_of_work`` dependencies through a
    connection whose top-level transaction rolls back on teardown. Row
    inserts issued by the endpoint under test are therefore never
    visible outside the fixture — the shared Supabase instance stays
    clean between tests.

    Implementation notes:

    * We override ``container.get_session`` and
      ``container.get_unit_of_work`` (both the *symbols* — FastAPI
      does dependency-identity matching, and both are wired through
      the ``Depends(container.get_x)`` signatures in
      ``app.api.v1.deps``).
    * The override yields sessions bound to the same **connection**
      as the ``session`` fixture would — but a fresh session per call
      so FastAPI's request-scoped DI still gets independent sessions.
    * The outer ``engine.connect()`` opens one connection for the
      whole client's lifetime; its top-level transaction is rolled back
      on teardown. All INSERTs across all requests during the test
      are inside that single transaction and vanish at the end.
    """
    from types import TracebackType
    from typing import Self as _Self

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.application.interfaces.repositories import (
        IProjectRepository,
        IProjectVersionRepository,
        IRoleRepository,
        ISceneRepository,
        ISessionRepository,
        ITenantRepository,
        IUserRepository,
    )
    from app.application.interfaces.unit_of_work import IUnitOfWork
    from app.infrastructure.repositories.project_repository import ProjectRepository
    from app.infrastructure.repositories.project_version_repository import (
        ProjectVersionRepository,
    )
    from app.infrastructure.repositories.role_repository import RoleRepository
    from app.infrastructure.repositories.scene_repository import SceneRepository
    from app.infrastructure.repositories.session_repository import SessionRepository
    from app.infrastructure.repositories.tenant_repository import TenantRepository
    from app.infrastructure.repositories.user_repository import UserRepository

    container.reset()
    container.init(settings)
    # Import inside the fixture body so loading ``app.main`` is deferred
    # until after ``_ensure_test_env_defaults`` has populated the env.
    from app.main import create_app

    app = create_app(settings)

    async with engine.connect() as connection:
        outer_tx = await connection.begin()
        # A per-connection sessionmaker — every request-scoped session
        # binds to the same underlying connection so all mutations sit
        # in the outer transaction that we roll back at teardown.
        test_session_factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )

        class _TestUnitOfWork(IUnitOfWork):
            """UoW that uses the test connection + creates savepoints per __aenter__."""

            def __init__(self) -> None:
                self._session: AsyncSession | None = None
                self._savepoint: Any = None
                self._committed = False

            async def __aenter__(self) -> _Self:
                self._session = test_session_factory()
                # commit() on the session commits the inner SAVEPOINT
                # only — the outer transaction stays open. That's what
                # we want: the endpoint under test can commit, but the
                # test still rolls everything back on teardown.
                self._committed = False
                self.users = cast(IUserRepository, UserRepository(self._session))
                self.tenants = cast(ITenantRepository, TenantRepository(self._session))
                self.sessions = cast(ISessionRepository, SessionRepository(self._session))
                self.roles = cast(IRoleRepository, RoleRepository(self._session))
                self.projects = cast(IProjectRepository, ProjectRepository(self._session))
                self.scenes = cast(ISceneRepository, SceneRepository(self._session))
                self.versions = cast(
                    IProjectVersionRepository, ProjectVersionRepository(self._session)
                )
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
                assert self._session is not None
                await self._session.commit()
                self._committed = True

            async def rollback(self) -> None:
                if self._session is not None:
                    await self._session.rollback()

        async def _override_get_session() -> AsyncIterator[AsyncSession]:
            async with test_session_factory() as sess:
                yield sess

        def _override_get_uow() -> IUnitOfWork:
            return _TestUnitOfWork()

        app.dependency_overrides[container.get_session] = _override_get_session
        app.dependency_overrides[container.get_unit_of_work] = _override_get_uow

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac

        app.dependency_overrides.clear()
        await outer_tx.rollback()

    await container.shutdown()
