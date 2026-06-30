"""Async SQLAlchemy engine + session factory builders.

Pure constructors — the singletons themselves live in
``app.core.container``. Tests instantiate their own engine via
``make_engine`` directly.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str) -> AsyncEngine:
    """Build an async engine with sensible defaults.

    ``pool_pre_ping`` detects stale connections from Supabase /
    PgBouncer-style poolers before they reach the application.
    """
    return create_async_engine(database_url, future=True, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the per-process session factory bound to an engine.

    ``expire_on_commit=False`` keeps loaded ORM attributes accessible
    after commit — the standard pattern for FastAPI handlers that
    return the just-committed object to the client.
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
