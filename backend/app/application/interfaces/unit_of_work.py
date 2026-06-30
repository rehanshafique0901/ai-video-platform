"""Port: Unit of Work — transaction boundary abstraction.

A concrete implementation lives in
``app.infrastructure.uow.sqlalchemy_unit_of_work``. Use cases depend
only on this interface so they remain free of SQLAlchemy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self


class IUnitOfWork(ABC):
    """Async context manager that owns one transactional boundary.

    Usage::

        async with uow:
            # ... do work via uow.session ...
            await uow.commit()

    Exit without an explicit ``commit()`` (or with an exception) rolls
    back. ``__aexit__`` always closes the underlying session.
    """

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
