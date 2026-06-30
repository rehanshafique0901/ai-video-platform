"""Application DI container — composition root.

This module is the *only* one under ``app.core`` that imports from
``app.infrastructure``. The API layer reaches infrastructure through
the container's accessors and therefore satisfies the import-linter
contract that forbids ``app.api`` from importing infrastructure
directly (see ``pyproject.toml`` ``[[tool.importlinter.contracts]]``).

Lifecycle:

1. ``init(settings)`` is called by ``app.main.create_app`` — builds
   the async engine, session factory, password hasher, and JWT service
   as process-wide singletons.
2. ``get_session`` / ``get_unit_of_work`` / ``get_user_repository``
   are imported by ``app.api.v1.deps`` and used as FastAPI
   dependencies.
3. ``shutdown()`` is called by the lifespan handler on process exit
   to dispose the engine cleanly.
4. ``reset()`` is provided for tests; it clears all singletons so the
   next ``init`` rebuilds them against test settings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.repositories import IUserRepository
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.config import Settings
from app.infrastructure.db.session import make_engine, make_session_factory
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.security.jwt import JWTService
from app.infrastructure.security.password_hasher import PasswordHasher
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_password_hasher: PasswordHasher | None = None
_jwt_service: JWTService | None = None


def init(settings: Settings) -> None:
    """Build the process-wide singletons.

    Idempotent: a second call with the container already initialised is
    a no-op. Tests that want to rebuild with different settings should
    call ``reset()`` first.
    """
    global _engine, _session_factory, _password_hasher, _jwt_service
    if _engine is not None:
        return
    _engine = make_engine(settings.database_url)
    _session_factory = make_session_factory(_engine)
    _password_hasher = PasswordHasher()
    _jwt_service = JWTService(
        secret=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
        access_ttl_seconds=settings.jwt_access_ttl_seconds,
        refresh_ttl_seconds=settings.jwt_refresh_ttl_seconds,
    )


async def shutdown() -> None:
    """Dispose the engine. Called by ``app.main``'s lifespan handler."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def reset() -> None:
    """Test-only: clear all singletons so the next ``init`` rebuilds them."""
    global _engine, _session_factory, _password_hasher, _jwt_service
    _engine = None
    _session_factory = None
    _password_hasher = None
    _jwt_service = None


def _require_init() -> None:
    if _engine is None:
        raise RuntimeError(
            "container not initialised — call init(settings) first "
            "(usually done by app.main.create_app)"
        )


def get_engine() -> AsyncEngine:
    _require_init()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    _require_init()
    assert _session_factory is not None
    return _session_factory


def get_password_hasher() -> PasswordHasher:
    _require_init()
    assert _password_hasher is not None
    return _password_hasher


def get_jwt_service() -> JWTService:
    _require_init()
    assert _jwt_service is not None
    return _jwt_service


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield one ``AsyncSession`` per request.

    The session is closed automatically. Commits are the caller's
    responsibility — for transactional mutations, acquire a UnitOfWork
    via ``get_unit_of_work`` instead.
    """
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_unit_of_work() -> IUnitOfWork:
    """FastAPI dependency: a fresh UnitOfWork bound to the session factory."""
    return SqlAlchemyUnitOfWork(get_session_factory())


def get_user_repository(session: AsyncSession) -> IUserRepository:
    """Factory: a ``UserRepository`` over the supplied session."""
    return UserRepository(session)
