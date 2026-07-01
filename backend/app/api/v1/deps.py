"""FastAPI dependency providers.

Imports are restricted to ``app.application.interfaces`` (ports) and
``app.core.container`` (composition root). Direct imports from
``app.infrastructure`` would violate the import-linter contract — the
container exists to hold the binding between the two so the API layer
never sees the implementations.

Slice α2a adds use-case dependency aliases so router handlers can
declare e.g. ``register_use_case: RegisterUserDep`` and receive a
fully-wired instance from the container.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IUserRepository
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.login_user import LoginUser
from app.application.use_cases.auth.logout_session import LogoutSession
from app.application.use_cases.auth.refresh_session import RefreshSession
from app.application.use_cases.auth.register_user import RegisterUser
from app.core import container
from app.core.errors import UnauthorizedError

SessionDep = Annotated[AsyncSession, Depends(container.get_session)]
UoWDep = Annotated[IUnitOfWork, Depends(container.get_unit_of_work)]


def get_user_repository(session: SessionDep) -> IUserRepository:
    """FastAPI sub-dependency: build a ``UserRepository`` from the request session."""
    return container.get_user_repository(session)


UserRepoDep = Annotated[IUserRepository, Depends(get_user_repository)]

# ---- Use-case dependencies (Slice α2a) --------------------------------

RegisterUserDep = Annotated[RegisterUser, Depends(container.get_register_user_use_case)]
LoginUserDep = Annotated[LoginUser, Depends(container.get_login_user_use_case)]

# ---- Use-case dependencies (Slice α2b) --------------------------------

RefreshSessionDep = Annotated[RefreshSession, Depends(container.get_refresh_session_use_case)]
LogoutSessionDep = Annotated[LogoutSession, Depends(container.get_logout_session_use_case)]


def _bearer_access_token(authorization: str | None = Header(default=None)) -> str:
    """Extract the raw access token from an ``Authorization: Bearer …`` header.

    Raises ``UnauthorizedError`` (401 via the app error handler) for a
    missing / malformed header. Cryptographic validation is the
    downstream use case's job — this dependency only parses the header
    shape.
    """
    if not authorization:
        raise UnauthorizedError("missing authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("malformed authorization header")
    return token


BearerAccessTokenDep = Annotated[str, Depends(_bearer_access_token)]
