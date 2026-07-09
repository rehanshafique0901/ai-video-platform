"""Unit tests for ``UpdateUserProfile`` (Slice α4).

Coverage map (α4 pre-flight §5.1):

* U1 — happy path: real change, ``version+1``, ``changed=True``, one commit.
* U2 — version mismatch raises :class:`VersionConflictError`, no commit,
  no side-effects.
* U3 — same-value no-op: repository echoes the pre-update row, use case
  returns ``changed=False``, no version bump.
* U4 — missing user (soft-deleted or never existed) collapses into the
  same :class:`VersionConflictError` as version mismatch (α4 §A10
  anti-enumeration invariant).
* U5 — use case does NOT transform ``display_name`` — the repository
  observes the exact string the caller passed. Defence against a
  future maintainer sneaking a ``.strip()`` into the use case (that's
  the DTO's job at the API boundary).
* U6 — happy path emits ``user.profile.updated`` (INFO) with the
  full field set from §A11.
* U7 — version mismatch emits ``user.profile.update_rejected`` (WARN)
  with ``reason=version_mismatch``.
* U8 — same-value no-op emits ``user.profile.update_rejected`` (INFO,
  not WARN — §A11 log-level table) with ``reason=same_value_noop``.

Cross-package import note: the auth ``_fakes`` module is the canonical
in-memory-fake surface for the whole application layer. α4 does not
duplicate it into a ``users/_fakes.py``; when the fakes ever need a
second consumer's worth of divergence, we lift the shared portion
into ``tests/unit/application/use_cases/_fakes.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.users.update_profile import (
    UpdateUserProfile,
    UpdateUserProfileResult,
)
from app.core.errors import VersionConflictError
from app.domain.identity.user import User
from tests.unit.application.use_cases.auth._fakes import (
    FakeUnitOfWork,
    FakeUserRepository,
)

# ---- Fixtures / helpers ----------------------------------------------


def _make_user(
    *,
    display_name: str = "Alice",
    version: int = 3,
) -> User:
    """Build a domain :class:`User` with sensible defaults for the fake."""
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        tenant_id=uuid4(),
        email="alice@example.com",
        password_hash="hash::pw",
        display_name=display_name,
        email_verified_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
        version=version,
    )


def _build_uc(
    seeded_user: User | None = None,
) -> tuple[UpdateUserProfile, FakeUnitOfWork, FakeUserRepository]:
    """Wire an ``UpdateUserProfile`` with an in-memory UoW + user repo.

    If ``seeded_user`` is provided, it is placed directly into the
    fake repo's ``_rows`` dict (bypassing ``add`` so we don't
    accidentally exercise the (tenant_id, email) uniqueness path).
    """
    users = FakeUserRepository()
    if seeded_user is not None:
        users._rows[seeded_user.id] = seeded_user
        users._by_email[seeded_user.email] = seeded_user.id
    uow = FakeUnitOfWork(users=users)
    uc = UpdateUserProfile(uow=uow)
    return uc, uow, users


# ---- U1 — happy path -------------------------------------------------


@pytest.mark.unit
async def test_u1_happy_path_bumps_version_and_updates_display_name() -> None:
    user = _make_user(display_name="Alice", version=3)
    uc, uow, users = _build_uc(seeded_user=user)

    result = await uc.execute(
        user_id=user.id,
        expected_version=3,
        display_name="Alice Cooper",
    )

    assert isinstance(result, UpdateUserProfileResult)
    assert result.changed is True
    assert result.user.id == user.id
    assert result.user.display_name == "Alice Cooper"
    assert result.user.version == 4
    assert result.user.updated_at >= user.updated_at

    # The fake was actually written to (not just returned by value).
    persisted = users._rows[user.id]
    assert persisted.display_name == "Alice Cooper"
    assert persisted.version == 4

    # Exactly one commit on the happy path.
    assert uow.commits == 1
    assert uow.rollbacks == 0


# ---- U2 — version mismatch → VersionConflictError --------------------


@pytest.mark.unit
async def test_u2_version_mismatch_raises_and_does_not_commit() -> None:
    user = _make_user(display_name="Alice", version=3)
    uc, uow, users = _build_uc(seeded_user=user)

    with pytest.raises(VersionConflictError) as exc_info:
        await uc.execute(
            user_id=user.id,
            expected_version=99,  # stale
            display_name="Alice Cooper",
        )

    # Anti-enumeration message (matches α4 §D6 wire spec).
    assert exc_info.value.message == "Resource has been modified."
    assert exc_info.value.code == "VERSION_CONFLICT"
    assert exc_info.value.http_status == 412

    # No side effects — row unchanged, no commit.
    assert users._rows[user.id].display_name == "Alice"
    assert users._rows[user.id].version == 3
    assert uow.commits == 0


# ---- U3 — same-value no-op -------------------------------------------


@pytest.mark.unit
async def test_u3_same_value_returns_changed_false_and_no_version_bump() -> None:
    user = _make_user(display_name="Alice", version=3)
    uc, uow, users = _build_uc(seeded_user=user)

    result = await uc.execute(
        user_id=user.id,
        expected_version=3,
        display_name="Alice",  # identical to seeded value
    )

    assert result.changed is False
    assert result.user.version == 3  # preserved, per §D6a invariant
    assert result.user.display_name == "Alice"

    # The persisted row is untouched by the no-op path (§D8).
    persisted = users._rows[user.id]
    assert persisted is user  # exact same object, no dataclasses.replace
    assert persisted.version == 3
    assert persisted.updated_at == user.updated_at

    # Commit still runs (the UoW is entered; commit-on-empty-TX is a
    # legal no-op and simpler than branching around it).
    assert uow.commits == 1


# ---- U4 — missing user collapses into VersionConflictError -----------


@pytest.mark.unit
async def test_u4_missing_user_raises_indistinguishable_version_conflict() -> None:
    uc, uow, users = _build_uc()  # empty repo — user does not exist
    orphan_id = uuid4()

    with pytest.raises(VersionConflictError) as exc_info:
        await uc.execute(
            user_id=orphan_id,
            expected_version=5,
            display_name="Anything",
        )

    # Same error type, same code, same message as U2 — a client cannot
    # distinguish "your version is stale" from "your account is gone"
    # (α4 §A10).
    assert exc_info.value.code == "VERSION_CONFLICT"
    assert exc_info.value.message == "Resource has been modified."
    assert uow.commits == 0
    assert orphan_id not in users._rows


# ---- U5 — use case is transparent on display_name --------------------


@pytest.mark.unit
async def test_u5_use_case_does_not_transform_display_name() -> None:
    """The use case treats ``display_name`` as opaque.

    Trimming/normalisation is the DTO's job at the API boundary
    (Pydantic ``str_strip_whitespace``). Adding a defensive
    ``.strip()`` here would silently mask a broken DTO.
    """
    user = _make_user(display_name="Alice", version=3)
    uc, _, users = _build_uc(seeded_user=user)

    padded = "  Alice Cooper  "
    result = await uc.execute(
        user_id=user.id,
        expected_version=3,
        display_name=padded,
    )

    # Verbatim — no strip, no normalisation.
    assert result.user.display_name == padded
    assert users._rows[user.id].display_name == padded


# ---- U6 — happy-path log ---------------------------------------------


@pytest.mark.unit
async def test_u6_happy_path_emits_user_profile_updated_log() -> None:
    user = _make_user(display_name="Alice", version=3)
    uc, _, _ = _build_uc(seeded_user=user)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            user_id=user.id,
            expected_version=3,
            display_name="Alice Cooper",
            ip="203.0.113.7",
        )

    updated_events = [e for e in logs if e.get("event") == "user.profile.updated"]
    assert len(updated_events) == 1, f"expected exactly one updated event, got {logs}"

    ev = updated_events[0]
    assert ev["log_level"] == "info"
    assert ev["user_id"] == str(user.id)
    assert ev["changed_fields"] == ["display_name"]
    assert ev["previous_version"] == 3
    assert ev["new_version"] == 4
    assert ev["ip"] == "203.0.113.7"

    # NEVER the value itself — pre-flight Q3 resolution.
    assert "Alice Cooper" not in str(ev)
    assert "display_name_value" not in ev


# ---- U7 — version-mismatch log ---------------------------------------


@pytest.mark.unit
async def test_u7_version_mismatch_emits_rejected_warn_log() -> None:
    user = _make_user(display_name="Alice", version=3)
    uc, _, _ = _build_uc(seeded_user=user)

    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(VersionConflictError),
    ):
        await uc.execute(
            user_id=user.id,
            expected_version=99,
            display_name="Alice Cooper",
            ip="203.0.113.7",
        )

    rejected = [e for e in logs if e.get("event") == "user.profile.update_rejected"]
    assert len(rejected) == 1

    ev = rejected[0]
    assert ev["log_level"] == "warning"
    assert ev["reason"] == "version_mismatch"
    assert ev["user_id"] == str(user.id)
    assert ev["expected_version"] == 99
    assert ev["ip"] == "203.0.113.7"

    # No `updated` event on the failure path.
    assert not any(e.get("event") == "user.profile.updated" for e in logs)


# ---- U8 — same-value-noop log ----------------------------------------


@pytest.mark.unit
async def test_u8_same_value_noop_emits_rejected_info_log() -> None:
    user = _make_user(display_name="Alice", version=3)
    uc, _, _ = _build_uc(seeded_user=user)

    with structlog.testing.capture_logs() as logs:
        await uc.execute(
            user_id=user.id,
            expected_version=3,
            display_name="Alice",
            ip="203.0.113.7",
        )

    rejected = [e for e in logs if e.get("event") == "user.profile.update_rejected"]
    assert len(rejected) == 1

    ev = rejected[0]
    # INFO — not WARN. The same-value no-op is a legitimate outcome,
    # not a security event (α4 §A11 log-level table).
    assert ev["log_level"] == "info"
    assert ev["reason"] == "same_value_noop"
    assert ev["user_id"] == str(user.id)
    assert ev["ip"] == "203.0.113.7"

    # No `updated` event — the version-increment invariant (§D6a)
    # applies to log events too: no real mutation, no `updated`.
    assert not any(e.get("event") == "user.profile.updated" for e in logs)
