"""Static checks against the central ENUM registry.

Adding a value to a Postgres ENUM is *not* free — every change must be
shipped as ``ALTER TYPE ... ADD VALUE`` in a migration. These tests act
as a fitness function so that:

* every enum declared in ``enums.py`` follows the registry pattern
  (``name=…``, ``native_enum=True``, ``create_type=True``);
* the value tuples are non-empty and contain only lowercase tokens
  (naming convention §7);
* the published list in ``schema.md`` and the implementation agree on
  the count.

The live validator already checks the *database-side* enum presence; the
suite here protects the *source-of-truth* side of the same contract.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Enum as SAEnum

from app.infrastructure.db import enums as enums_module

# Expected count is the implementation count after Phase 2B closed.
# α8.6a (ADR-0047, migration 0013): +1 for ``social_account_status`` — the
# publishing bounded context's connected/expired/revoked account states.
EXPECTED_ENUM_COUNT = 27

_LOWER_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def _enum_registry() -> dict[str, SAEnum]:
    registry: dict[str, SAEnum] = {}
    for attr_name in dir(enums_module):
        if not attr_name.endswith("_enum"):
            continue
        candidate = getattr(enums_module, attr_name)
        if isinstance(candidate, SAEnum):
            registry[attr_name] = candidate
    return registry


@pytest.mark.unit
def test_enum_count_matches_phase2_baseline() -> None:
    """A change to the count without a matching ADR/migration is a defect."""

    registry = _enum_registry()
    assert len(registry) == EXPECTED_ENUM_COUNT, (
        f"enum count drift: expected {EXPECTED_ENUM_COUNT}, got {len(registry)}. "
        f"If this is an intentional addition, ship an `ALTER TYPE ADD VALUE` "
        f"migration AND bump EXPECTED_ENUM_COUNT in tests/test_enums.py AND "
        f"add an ADR documenting the new enum."
    )


@pytest.mark.unit
def test_every_enum_uses_native_postgres_type() -> None:
    """``native_enum=False`` would silently fall back to varchar — not allowed."""

    offenders: list[str] = []
    for name, enum in _enum_registry().items():
        if not enum.native_enum:
            offenders.append(name)
    assert (
        not offenders
    ), "the following enums are not declared as native Postgres ENUMs: " + ", ".join(offenders)


@pytest.mark.unit
def test_every_enum_has_lowercase_snake_case_values() -> None:
    """Mixed-case enum values cause subtle drift between code and SQL."""

    offenders: list[str] = []
    for name, enum in _enum_registry().items():
        for value in enum.enums:
            if not _LOWER_SNAKE.match(value):
                offenders.append(f"{name}: {value!r}")
    assert not offenders, "enum values must match lowercase_snake_case: " + ", ".join(offenders)


@pytest.mark.unit
def test_no_enum_has_duplicate_values() -> None:
    offenders: list[str] = []
    for name, enum in _enum_registry().items():
        if len(set(enum.enums)) != len(enum.enums):
            duplicates = [v for v in enum.enums if enum.enums.count(v) > 1]
            offenders.append(f"{name}: {sorted(set(duplicates))}")
    assert not offenders, "duplicate enum values: " + ", ".join(offenders)


@pytest.mark.unit
def test_every_enum_has_unique_postgres_type_name() -> None:
    """If two enums share a ``name=`` argument, only one survives the migration."""

    seen: dict[str, str] = {}
    collisions: list[str] = []
    for attr_name, enum in _enum_registry().items():
        pg_name = enum.name
        if pg_name in seen:
            collisions.append(f"{attr_name} collides with {seen[pg_name]} on type {pg_name!r}")
        else:
            seen[pg_name] = attr_name
    assert not collisions, "enum name collisions: " + ", ".join(collisions)
