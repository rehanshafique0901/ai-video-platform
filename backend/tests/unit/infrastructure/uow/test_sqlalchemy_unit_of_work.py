"""Unit tests for ``SqlAlchemyUnitOfWork`` — DB-free, mocked session."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork


@pytest.mark.unit
def test_session_property_raises_when_uow_not_active() -> None:
    factory = MagicMock()
    uow = SqlAlchemyUnitOfWork(factory)
    with pytest.raises(RuntimeError, match="not active"):
        _ = uow.session


@pytest.mark.unit
async def test_uow_commits_when_explicit() -> None:
    session = AsyncMock()
    factory = MagicMock(return_value=session)
    uow = SqlAlchemyUnitOfWork(factory)

    async with uow:
        await uow.commit()

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.unit
async def test_uow_rolls_back_on_exception() -> None:
    session = AsyncMock()
    factory = MagicMock(return_value=session)
    uow = SqlAlchemyUnitOfWork(factory)

    with pytest.raises(ValueError):
        async with uow:
            raise ValueError("boom")

    session.rollback.assert_awaited()
    session.commit.assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.unit
async def test_uow_rolls_back_when_no_commit_called() -> None:
    session = AsyncMock()
    factory = MagicMock(return_value=session)
    uow = SqlAlchemyUnitOfWork(factory)

    async with uow:
        pass  # forgot to commit

    session.rollback.assert_awaited()
    session.commit.assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.unit
async def test_uow_session_accessible_inside_block() -> None:
    session = AsyncMock()
    factory = MagicMock(return_value=session)
    uow = SqlAlchemyUnitOfWork(factory)

    async with uow:
        assert uow.session is session
        await uow.commit()
