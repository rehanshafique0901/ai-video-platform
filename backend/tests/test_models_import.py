"""Every ORM model module imports cleanly without DB I/O.

This is the cheapest possible "smoke" test: it proves the production
package boots end-to-end on a fresh checkout, that no circular import
slipped in, and that every model class registers itself onto the shared
``Base.metadata``. A regression here will trip stage 4 of the CI gate
before stages 5–9 have a chance to touch a real database.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import app.infrastructure.db.models as models_pkg
from app.infrastructure.db import Base, metadata


@pytest.mark.unit
def test_models_package_is_importable() -> None:
    assert models_pkg is not None
    assert hasattr(models_pkg, "__path__"), "models is expected to be a package"


@pytest.mark.unit
def test_every_model_submodule_imports() -> None:
    """Walk ``app.infrastructure.db.models`` and import every submodule.

    Any ImportError, SyntaxError, or circular-import collision will
    surface here. The assertion is non-empty so an empty package
    accidentally introduced by a refactor would also fail the test.
    """

    discovered: list[str] = []
    for module_info in pkgutil.iter_modules(models_pkg.__path__):
        full_name = f"{models_pkg.__name__}.{module_info.name}"
        importlib.import_module(full_name)
        discovered.append(full_name)
    assert discovered, "expected at least one model submodule"


@pytest.mark.unit
def test_metadata_contains_known_tables() -> None:
    """Sanity check on key aggregate-root tables.

    We don't pin the full table list here (that's what
    ``validate_schema.py`` does against a live database). We do assert a
    representative subset is mapped, so any regression that drops a
    model file entirely is caught at unit-test time.
    """

    table_names = set(metadata.tables.keys())
    expected_subset = {
        "tenants",
        "users",
        "projects",
        "project_versions",
        "media_assets",
        "library_assets",
        "render_jobs",
        "workflow_runs",
        "ai_models",
        "ai_model_pricing",
        "credit_ledger",
        "audit_log",
        "event_outbox",
        "feature_flags",
    }
    missing = expected_subset - table_names
    assert not missing, f"metadata is missing expected tables: {sorted(missing)}"


@pytest.mark.unit
def test_base_is_declarative() -> None:
    """The Base must expose SQLAlchemy declarative entry points.

    The CI gate's static analysis can't catch a misconfigured
    ``DeclarativeBase`` subclass; this test makes it explicit.
    """

    assert hasattr(Base, "registry"), "Base must expose a SQLAlchemy registry"
    assert hasattr(Base, "metadata"), "Base must expose a metadata attribute"
    assert (
        Base.metadata is metadata
    ), "Base.metadata and the exported metadata must be the same object"
