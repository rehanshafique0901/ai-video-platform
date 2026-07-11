"""Unit tests for ``RefreshSession`` (Slice α2b).

Covers the 12 acceptance-criteria items catalogued in the α2b pre-flight
§5, using in-memory fakes so nothing here touches Argon2 / SQLAlchemy /
PyJWT. Every test that manipulates time uses ``FakeClock(fixed_at=...)``
so assertions on ``revoked_at`` timestamps are exact.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.security import IssuedTokens
from app.application.use_cases.auth.errors import InvalidRefreshTokenError
from app.application.use_cases.auth.refresh_session import RefreshSession
from app.domain.identity.session import Session
from app.domain.identity.user import User

from ._fakes import (
    FakeClock,
    FakePasswordHasher,
    FakeSessionRepository,
    FakeTokenIssuer,
    FakeUnitOfWork,
    FakeUserRepository,
)

# ---- Helpers ----------------------------------------------------------

_T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _make_user() -> User:
    hasher = FakePasswordHasher()
    return User(
        id=uuid4(),
        tenant_id=uuid4(),
        email="ref@example.com",
        password_hash=hasher.hash("pw"),
        display_name="Refresh User",
        email_verified_at=None,
        last_login_at=None,
        created_at=_T0,
        updated_at=_T0,
        version=1,
    )


async def _seed_login(
    *,
    user: User,
    sessions: FakeSessionRepository,
    users: FakeUserRepository,
    issuer: FakeTokenIssuer,
) -> IssuedTokens:
    """Simulate a prior login: put the user in the fake repo and mint a session."""
    await users.add(user)
    tokens = issuer.issue_for_login(user)
    await sessions.add(
        Session(
            id=tokens.session_id,
            user_id=user.id,
            family_id=tokens.family_id,
            token_hash=tokens.refresh_token_hash,
            ip=None,
            user_agent=None,
            issued_at=tokens.issued_at,
            last_used_at=tokens.issued_at,
            expires_at=tokens.refresh_expires_at,
            revoked_at=None,
        )
    )
    return tokens


def _make_use_case(
    *,
    users: FakeUserRepository,
    sessions: FakeSessionRepository,
    issuer: FakeTokenIssuer,
    clock: FakeClock,
) -> tuple[RefreshSession, FakeUnitOfWork]:
    uow = FakeUnitOfWork(users=users, sessions=sessions)
    uc = RefreshSession(uow=uow, token_issuer=issuer, clock=clock)
    return uc, uow


# ---- Happy path ------------------------------------------------------


@pytest.mark.unit
async def test_happy_path_rotates_family_and_returns_fresh_tokens() -> None:
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    original = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)

    uc, uow = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    result = await uc.execute(refresh_token=original.refresh_token)

    # New tokens, same family, different sid.
    assert result.tokens.refresh_token != original.refresh_token
    assert result.tokens.access_token != original.access_token
    assert result.tokens.family_id == original.family_id
    assert result.tokens.session_id != original.session_id

    # Old row revoked at clock time; new row live.
    old_row = sessions._rows[original.session_id]
    new_row = sessions._rows[result.tokens.session_id]
    assert old_row.revoked_at == _T0
    assert new_row.revoked_at is None
    assert new_row.family_id == original.family_id

    # UoW committed exactly once.
    assert uow.commits == 1


# ---- Verify-time rejections ------------------------------------------


@pytest.mark.unit
async def test_bad_signature_raises_invalid_refresh_token() -> None:
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    uc, uow = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)

    # The fake issuer's verify_refresh raises UnauthorizedError for any
    # token it did not itself issue (no _register call). RefreshSession
    # must translate that into the generic InvalidRefreshTokenError so
    # the client-facing message never leaks the specific verify failure.
    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token="never.issued.by.us")

    assert uow.commits == 0


# ---- Hash / lookup / consistency -------------------------------------


@pytest.mark.unit
async def test_hash_miss_raises_invalid_refresh_token() -> None:
    """A JWT with a valid signature but no matching session row → 401.

    Simulates a session that was purged from the DB while the token was
    still in-hand (rare but possible: hard-delete for GDPR, DB restore
    from before the session's issuance, etc.).
    """
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    await users.add(user)
    orphan_tokens = issuer.issue_for_login(user)  # registered with issuer, but no session row

    uc, uow = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token=orphan_tokens.refresh_token)

    assert uow.commits == 0


@pytest.mark.unit
async def test_sid_mismatch_raises_and_leaves_family_alive() -> None:
    """Defence in depth: hash matches but the JWT claims a different sid.

    Achieved here by hand-manipulating the fake issuer's claim registry
    to point a token at a different session_id than the one on the row.
    The reuse-detection path must NOT fire (this is not a rotation
    replay signal — it's a malformed / tampered token), so the family
    stays alive.
    """
    from app.application.interfaces.security import TokenClaims

    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    original = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)

    # Rewrite the claim registry so the refresh token now advertises a
    # different sid than the row it hashes into.
    issuer._claims_by_token[original.refresh_token] = TokenClaims(
        subject=user.id,
        session_id=uuid4(),  # ← lies about which session this token belongs to
        family_id=original.family_id,
        expires_at=original.refresh_expires_at,
    )

    uc, uow = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token=original.refresh_token)

    # Family untouched — sid mismatch is not a reuse signal.
    row = sessions._rows[original.session_id]
    assert row.revoked_at is None
    assert sessions.list_family_calls == []  # no family scan happened
    assert uow.commits == 0


# ---- Reuse detection -------------------------------------------------


@pytest.mark.unit
async def test_replayed_rotated_token_revokes_the_whole_family() -> None:
    """The critical security property. Full lifecycle in one test.

    Chronology:
      t0  register/login       → family F, session S1
      t0  first refresh(R1)    → rotates to S2 (R1 now revoked)
      t0  ATTACKER replays R1  → hash matches S1, revoked_at != NULL
                                → revoke every live sibling in F
                                → 401 to attacker
      Post: S1 revoked (t0 already), S2 revoked (this call), R2 dead too.
    """
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    tokens_1 = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)

    # First refresh: happy path.
    uc, uow = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    result_1 = await uc.execute(refresh_token=tokens_1.refresh_token)
    tokens_2 = result_1.tokens
    assert sessions._rows[tokens_1.session_id].revoked_at is not None
    assert sessions._rows[tokens_2.session_id].revoked_at is None

    # Attacker replays R1. Advance the clock so the family-revoke uses a
    # different timestamp than the original rotation.
    clock.tick(60)  # +60 seconds
    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token=tokens_1.refresh_token)

    # Both sessions in the family are now revoked. The first row keeps
    # its original revoked_at (CAS semantics); the second row was
    # revoked *by the reuse-detection path* at t0+60s.
    r1 = sessions._rows[tokens_1.session_id]
    r2 = sessions._rows[tokens_2.session_id]
    assert r1.revoked_at == _T0
    assert r2.revoked_at == _T0 + timedelta(seconds=60)

    # A subsequent rotation attempt with R2 (previously valid) must
    # also fail — the whole family is dead.
    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token=tokens_2.refresh_token)


@pytest.mark.unit
async def test_reuse_detection_leaves_other_families_alone() -> None:
    """Two independent devices == two families. Compromising one must not touch the other."""
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    device_a = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)
    device_b_tokens = issuer.issue_for_login(user)  # simulate a second device login
    await sessions.add(
        Session(
            id=device_b_tokens.session_id,
            user_id=user.id,
            family_id=device_b_tokens.family_id,
            token_hash=device_b_tokens.refresh_token_hash,
            ip=None,
            user_agent=None,
            issued_at=device_b_tokens.issued_at,
            last_used_at=device_b_tokens.issued_at,
            expires_at=device_b_tokens.refresh_expires_at,
            revoked_at=None,
        )
    )
    assert device_a.family_id != device_b_tokens.family_id

    # Rotate + then replay device A. Device B must stay alive.
    uc, _ = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    await uc.execute(refresh_token=device_a.refresh_token)
    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token=device_a.refresh_token)

    device_b_row = sessions._rows[device_b_tokens.session_id]
    assert device_b_row.revoked_at is None  # untouched


# ---- User liveness (A13) --------------------------------------------


@pytest.mark.unit
async def test_soft_deleted_user_yields_401_and_revokes_only_this_session() -> None:
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    tokens = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)

    # Simulate a soft-delete happening after the token was issued.
    # The fake repository's get_by_id returns None if the row is not in
    # _rows — the simplest way to model "gone from the tenant's view".
    del users._rows[user.id]

    uc, _ = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token=tokens.refresh_token)

    # This session revoked (housekeeping), but no family-scan happened.
    assert sessions._rows[tokens.session_id].revoked_at == _T0
    assert sessions.list_family_calls == []


# ---- Clock injection --------------------------------------------------


@pytest.mark.unit
async def test_revoke_timestamp_comes_from_injected_clock_not_wall_clock() -> None:
    users, sessions, issuer = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
    )
    frozen = FakeClock(fixed_at=_T0)
    user = _make_user()
    tokens = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)

    uc, _ = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=frozen)
    await uc.execute(refresh_token=tokens.refresh_token)

    assert sessions._rows[tokens.session_id].revoked_at == _T0


# ---- Structured logging ---------------------------------------------


@pytest.mark.unit
async def test_reuse_detection_logs_security_event_flag(capsys) -> None:  # type: ignore[no-untyped-def]
    """The reuse-detection warning MUST carry ``security_event=True`` for SIEM keying.

    ``structlog`` is configured (see ``app.core.logging``) to write its
    own rendered lines to stdout, bypassing Python's stdlib ``logging``
    module — so ``capsys`` captures it while ``caplog`` does not.
    """
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    tokens = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)

    # Force a reuse by pre-revoking the session row directly.
    import dataclasses

    row = sessions._rows[tokens.session_id]
    sessions._rows[tokens.session_id] = dataclasses.replace(row, revoked_at=_T0)

    uc, _ = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token=tokens.refresh_token)

    captured = capsys.readouterr()
    # ``structlog``'s dev renderer wraps values in ANSI colour codes;
    # strip them before substring-matching so the assertion is stable
    # regardless of terminal / CI colour settings.
    import re

    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    combined = ansi_re.sub("", captured.out + captured.err)
    assert "auth.refresh.reuse_detected" in combined
    assert "security_event=True" in combined


# ---- Refresh token hash + return-value contract ---------------------


@pytest.mark.unit
async def test_returned_hash_matches_sha256_of_returned_refresh_token() -> None:
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    tokens = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)

    uc, _ = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    result = await uc.execute(refresh_token=tokens.refresh_token)

    expected = hashlib.sha256(result.tokens.refresh_token.encode()).hexdigest()
    assert result.tokens.refresh_token_hash == expected
    assert result.session.token_hash == expected


# ---- User identity preserved across rotation ------------------------


@pytest.mark.unit
async def test_rotation_returns_the_same_user_entity() -> None:
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    tokens = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)

    uc, _ = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    result = await uc.execute(refresh_token=tokens.refresh_token)

    assert result.user.id == user.id
    assert result.user.email == user.email


# ---- Family_id is preserved across rotation -------------------------


@pytest.mark.unit
async def test_rotation_preserves_family_id_across_multiple_rounds() -> None:
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    user = _make_user()
    tokens = await _seed_login(user=user, sessions=sessions, users=users, issuer=issuer)
    original_family: UUID = tokens.family_id

    uc, _ = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)
    current = tokens
    seen_sids = {tokens.session_id}
    for _ in range(3):
        result = await uc.execute(refresh_token=current.refresh_token)
        assert result.tokens.family_id == original_family
        assert result.tokens.session_id not in seen_sids
        seen_sids.add(result.tokens.session_id)
        current = result.tokens


# ---- Verify-failure paths do not commit ------------------------------


@pytest.mark.unit
async def test_no_commit_on_any_verify_failure_path() -> None:
    """Every 401 (except reuse detection + user_gone) must leave UoW committed=0."""
    users, sessions, issuer, clock = (
        FakeUserRepository(),
        FakeSessionRepository(),
        FakeTokenIssuer(),
        FakeClock(fixed_at=_T0),
    )
    uc, uow = _make_use_case(users=users, sessions=sessions, issuer=issuer, clock=clock)

    with pytest.raises(InvalidRefreshTokenError):
        await uc.execute(refresh_token="unknown")
    assert uow.commits == 0
