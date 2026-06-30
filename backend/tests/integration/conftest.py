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
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """httpx ``AsyncClient`` wired to the FastAPI app for in-process testing.

    The app uses its own container-managed session factory rather than
    the test fixture's per-test SAVEPOINT session. For α1 this is fine
    — the only DB-touching handler is ``/readyz``, which just runs
    ``SELECT 1`` without writes. Slice α2+ will override the session
    dependency via ``app.dependency_overrides`` when handlers begin to
    mutate state.
    """
    container.reset()
    container.init(settings)
    # Import inside the fixture body so loading ``app.main`` is deferred
    # until after ``_ensure_test_env_defaults`` has populated the env.
    from app.main import create_app

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    await container.shutdown()
