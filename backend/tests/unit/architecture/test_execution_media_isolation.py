"""X8 guard (α8.8): the Execution Runtime never imports the media bounded context.

A fast, self-contained companion to the import-linter contract "Execution Runtime never
writes the media library (ADR-0046 X8)". It AST-scans the execution-plane source
packages and asserts none of them import ``app.domain.media``,
``app.application.use_cases.media``, or the media repository — so the *only* path from a
generation to ``media_assets`` is the explicit ``PromoteGenerationAssets`` bridge, which
lives in the media package and reads generations through the read-only
``IGenerationReader`` port. This runs in stage 4 (unit), catching a boundary regression
before the stage-3 import-linter run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app

pytestmark = pytest.mark.unit

_APP_ROOT = Path(app.__file__).resolve().parent

# The execution plane: its use cases + its infrastructure (including the read-only reader).
_EXECUTION_DIRS = (
    _APP_ROOT / "application" / "use_cases" / "generation",
    _APP_ROOT / "infrastructure" / "generation",
)

# Importing any of these from the execution plane would bypass the promotion bridge.
_FORBIDDEN_PREFIXES = (
    "app.domain.media",
    "app.application.use_cases.media",
    "app.infrastructure.repositories.media_repository",
)


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return modules


def _execution_python_files() -> list[Path]:
    files: list[Path] = []
    for directory in _EXECUTION_DIRS:
        assert directory.is_dir(), f"expected execution package dir: {directory}"
        files.extend(sorted(directory.rglob("*.py")))
    return files


def test_execution_plane_does_not_import_media() -> None:
    files = _execution_python_files()
    assert files, "expected to scan at least one execution-plane module"
    violations: list[str] = []
    for path in files:
        for module in _imported_modules(path.read_text()):
            if any(module == p or module.startswith(p + ".") for p in _FORBIDDEN_PREFIXES):
                violations.append(f"{path.relative_to(_APP_ROOT)} imports {module}")
    assert (
        not violations
    ), "execution plane must not import the media bounded context (X8):\n" + "\n".join(violations)
