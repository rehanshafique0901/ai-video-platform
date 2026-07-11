"""Application DI container — composition root.

This module is the *only* one under ``app.core`` that imports from
``app.infrastructure``. The API layer reaches infrastructure through
the container's accessors and therefore satisfies the import-linter
contract that forbids ``app.api`` from importing infrastructure
directly (see ``pyproject.toml`` ``[[tool.importlinter.contracts]]``).

Lifecycle:

1. ``init(settings)`` is called by ``app.main.create_app`` — builds
   the async engine, session factory, password hasher, JWT service,
   token issuer, and (α2a) pre-computes the anti-enumeration dummy
   Argon2 hash so the ``LoginUser`` use case doesn't pay a ~300 ms
   Argon2 cost on the first request per process.
2. ``get_session`` / ``get_unit_of_work`` and the various
   use-case factories are imported by ``app.api.v1.deps`` and used
   as FastAPI dependencies.
3. ``shutdown()`` is called by the lifespan handler on process exit
   to dispose the engine cleanly.
4. ``reset()`` is provided for tests; it clears all singletons so the
   next ``init`` rebuilds them against test settings.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.clock import IClock
from app.application.interfaces.repositories import IUserRepository
from app.application.interfaces.security import IPasswordHasher, ITokenIssuer
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.login_user import LoginUser
from app.application.use_cases.auth.logout_session import LogoutSession
from app.application.use_cases.auth.refresh_session import RefreshSession
from app.application.use_cases.auth.register_user import RegisterUser
from app.application.use_cases.projects.create_project import CreateProject
from app.application.use_cases.projects.delete_project import DeleteProject
from app.application.use_cases.projects.get_project import GetProject
from app.application.use_cases.projects.list_projects import ListProjects
from app.application.use_cases.projects.update_project import UpdateProject
from app.application.use_cases.users.update_profile import UpdateUserProfile
from app.core.config import Settings
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.session import make_engine, make_session_factory
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.security.jwt import JWTService
from app.infrastructure.security.password_hasher import PasswordHasher
from app.infrastructure.security.token_issuer import AuthTokenIssuer
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_password_hasher: PasswordHasher | None = None
_jwt_service: JWTService | None = None
_token_issuer: AuthTokenIssuer | None = None
_dummy_password_hash: str | None = None
_clock: SystemClock | None = None


def init(settings: Settings) -> None:
    """Build the process-wide singletons.

    Idempotent: a second call with the container already initialised is
    a no-op. Tests that want to rebuild with different settings should
    call ``reset()`` first.
    """
    global _engine, _session_factory, _password_hasher, _jwt_service
    global _token_issuer, _dummy_password_hash, _clock
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
    _token_issuer = AuthTokenIssuer(
        jwt_service=_jwt_service,
        refresh_ttl_seconds=settings.jwt_refresh_ttl_seconds,
    )
    # Anti-enumeration dummy hash: computed once at startup so
    # ``LoginUser`` never pays the ~300 ms Argon2 cost per request.
    # The plaintext is a discarded random 32-byte secret — even if an
    # attacker learned it, it would not help them since the hash is
    # never a real user's password.
    _dummy_password_hash = _password_hasher.hash(secrets.token_urlsafe(32))
    _clock = SystemClock()


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
    global _token_issuer, _dummy_password_hash, _clock
    _engine = None
    _session_factory = None
    _password_hasher = None
    _jwt_service = None
    _token_issuer = None
    _dummy_password_hash = None
    _clock = None


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


def get_password_hasher() -> IPasswordHasher:
    _require_init()
    assert _password_hasher is not None
    return _password_hasher


def get_jwt_service() -> JWTService:
    _require_init()
    assert _jwt_service is not None
    return _jwt_service


def get_token_issuer() -> ITokenIssuer:
    _require_init()
    assert _token_issuer is not None
    return _token_issuer


def get_dummy_password_hash() -> str:
    _require_init()
    assert _dummy_password_hash is not None
    return _dummy_password_hash


def get_clock() -> IClock:
    _require_init()
    assert _clock is not None
    return _clock


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


# ---------------------------------------------------------------------
# Use-case factories (Slice α2a)
# ---------------------------------------------------------------------
#
# Each factory constructs one use case with a fresh UoW + the shared
# security singletons. FastAPI-facing wrappers live in
# ``app.api.v1.deps`` so the router never imports this module.
# ---------------------------------------------------------------------


def get_register_user_use_case() -> RegisterUser:
    return RegisterUser(
        uow=get_unit_of_work(),
        hasher=get_password_hasher(),
        token_issuer=get_token_issuer(),
        clock=get_clock(),
    )


def get_login_user_use_case() -> LoginUser:
    return LoginUser(
        uow=get_unit_of_work(),
        hasher=get_password_hasher(),
        token_issuer=get_token_issuer(),
        dummy_password_hash=get_dummy_password_hash(),
        clock=get_clock(),
    )


def get_refresh_session_use_case() -> RefreshSession:
    return RefreshSession(
        uow=get_unit_of_work(),
        token_issuer=get_token_issuer(),
        clock=get_clock(),
    )


def get_logout_session_use_case() -> LogoutSession:
    return LogoutSession(
        uow=get_unit_of_work(),
        token_issuer=get_token_issuer(),
        clock=get_clock(),
    )


# ---------------------------------------------------------------------
# Use-case factories (Slice α4)
# ---------------------------------------------------------------------


def get_update_user_profile_use_case() -> UpdateUserProfile:
    """Factory: a fresh ``UpdateUserProfile`` use case bound to a new UoW.

    See pre-flight §4.1 (composition root wiring) and §12 step 8.
    """
    return UpdateUserProfile(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α5a — Projects create + read)
# ---------------------------------------------------------------------


def get_create_project_use_case() -> CreateProject:
    return CreateProject(uow=get_unit_of_work())


def get_get_project_use_case() -> GetProject:
    return GetProject(uow=get_unit_of_work())


def get_list_projects_use_case() -> ListProjects:
    return ListProjects(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α5b — Projects update + soft-delete)
# ---------------------------------------------------------------------


def get_update_project_use_case() -> UpdateProject:
    return UpdateProject(uow=get_unit_of_work())


def get_delete_project_use_case() -> DeleteProject:
    return DeleteProject(uow=get_unit_of_work())
