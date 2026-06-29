"""Shared pytest fixtures.

The Phase 2C smoke suite is intentionally lightweight — no database is
spun up. Integration tests (live PostgreSQL + pgvector) are owned by the
schema-validation harness under ``backend/scripts/`` and live in CI
stages 5–9, not in the pytest collection.

Fixtures here are deliberately conservative so future Phase 3 tests can
extend the suite without rewiring existing collateral.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def app_package_root() -> str:
    """Absolute import path of the production package under test.

    Used by tests that assert against the public model surface
    (``app.infrastructure.db.models.*``) without hard-coding paths.
    """

    return "app"


@pytest.fixture
def _isolation_marker() -> Iterator[None]:
    """Placeholder per-test isolation hook.

    Phase 3 will replace this with a transactional savepoint scoped to a
    test database. For now it exists so per-test setup is structured.
    """

    yield None
