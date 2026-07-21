"""FastAPI application factory (Slice α1 — Architecture Bootstrap).

Run locally (cross-platform, recommended on Windows)::

    python scripts/run_dev.py

The script bypasses the uvicorn CLI so it can pass an explicit
``loop_factory`` to ``asyncio.run`` — required on Windows because
``psycopg``'s async driver does not work with ``ProactorEventLoop``,
and Python 3.12+ ``asyncio.run(..., loop_factory=...)`` *bypasses*
the global event-loop policy. See ``scripts/run_dev.py`` for details.

On Linux / macOS the uvicorn CLI also works::

    uvicorn app.main:create_app --factory --reload --port 8000

The ``--factory`` flag tells uvicorn to call ``create_app()`` to build
the app rather than importing a module-level ``app`` symbol. This is
intentional: building the app at module-import time would require
environment variables (``DATABASE_URL``, ``JWT_SECRET``) to be loaded
first, which is awkward in tests. ``create_app(settings)`` lets the
integration-test fixture inject a test ``Settings`` instance
explicitly.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from sqlalchemy import text

from app.api.v1.routers import (
    auth,
    health,
    media,
    projects,
    prompts,
    render_jobs,
    scenes,
    timeline,
    users,
    versions,
    workflow_runs,
)
from app.core import container
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware

# Windows: psycopg's async driver does not support the default
# ProactorEventLoop. Switch to SelectorEventLoop at module-import time
# so uvicorn picks it up when it constructs the loop. No-op on
# Linux/macOS (production).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook.

    Startup: ping the DB engine so a missing or misconfigured
    ``DATABASE_URL`` fails fast at boot rather than on the first
    request. Shutdown: dispose the engine to drain pooled connections.
    """
    logger = structlog.get_logger(__name__)
    engine = container.get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("application_started")
    try:
        yield
    finally:
        await container.shutdown()
        logger.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct the FastAPI app.

    ``settings`` can be injected for test isolation; production uses
    the cached ``get_settings()`` default.
    """
    if settings is None:
        settings = get_settings()

    configure_logging(settings.log_level, settings.environment)
    container.init(settings)

    app = FastAPI(
        title="AI Video Platform — backend",
        version="0.4.23-phase3-alpha8.3",
        lifespan=lifespan,
    )

    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    # ``health`` stays at the root path per API_CONTRACT §2 (public
    # ``/healthz`` + ``/readyz`` are versionless). All other v1 surface
    # sits under ``/api/v1``.
    app.include_router(health.router)
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(projects.router, prefix="/api/v1")
    app.include_router(scenes.router, prefix="/api/v1")
    app.include_router(versions.router, prefix="/api/v1")
    app.include_router(prompts.router, prefix="/api/v1")
    app.include_router(media.router, prefix="/api/v1")
    app.include_router(timeline.router, prefix="/api/v1")
    app.include_router(render_jobs.router, prefix="/api/v1")
    app.include_router(workflow_runs.router, prefix="/api/v1")

    return app
