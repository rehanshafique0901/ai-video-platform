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

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IUserRepository
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.login_user import LoginUser
from app.application.use_cases.auth.register_user import RegisterUser
from app.core import container

SessionDep = Annotated[AsyncSession, Depends(container.get_session)]
UoWDep = Annotated[IUnitOfWork, Depends(container.get_unit_of_work)]


def get_user_repository(session: SessionDep) -> IUserRepository:
    """FastAPI sub-dependency: build a ``UserRepository`` from the request session."""
    return container.get_user_repository(session)


UserRepoDep = Annotated[IUserRepository, Depends(get_user_repository)]

# ---- Use-case dependencies (Slice α2a) --------------------------------

RegisterUserDep = Annotated[RegisterUser, Depends(container.get_register_user_use_case)]
LoginUserDep = Annotated[LoginUser, Depends(container.get_login_user_use_case)]
