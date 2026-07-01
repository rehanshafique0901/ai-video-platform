"""Unit tests for ``LoginUser`` (Slice α2a)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.use_cases.auth.errors import InvalidCredentialsError
from app.application.use_cases.auth.login_user import LoginUser
from app.domain.identity.user import User

from ._fakes import (
    FakePasswordHasher,
    FakeSessionRepository,
    FakeTokenIssuer,
    FakeUnitOfWork,
    FakeUserRepository,
)

_DUMMY = FakePasswordHasher().hash("__anti_enumeration__")


def _make_user(email: str, password: str, hasher: FakePasswordHasher) -> User:
    return User(
        id=uuid4(),
        tenant_id=uuid4(),
        email=email,
        password_hash=hasher.hash(password),
        display_name="Test User",
        email_verified_at=None,
        last_login_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        version=1,
    )


def _make_use_case(
    *,
    users: FakeUserRepository | None = None,
    hasher: FakePasswordHasher | None = None,
    dummy_hash: str = _DUMMY,
) -> tuple[LoginUser, FakeUnitOfWork, FakePasswordHasher]:
    h = hasher or FakePasswordHasher()
    uow = FakeUnitOfWork(users=users, sessions=FakeSessionRepository())
    uc = LoginUser(
        uow=uow, hasher=h, token_issuer=FakeTokenIssuer(), dummy_password_hash=dummy_hash
    )
    return uc, uow, h


@pytest.mark.unit
async def test_happy_path_returns_user_and_new_session() -> None:
    hasher = FakePasswordHasher()
    users = FakeUserRepository()
    user = _make_user("alice@example.com", "s3cret", hasher)
    await users.add(user)

    uc, uow, _ = _make_use_case(users=users, hasher=hasher)
    result = await uc.execute(email="alice@example.com", password="s3cret")

    assert result.user.id == user.id
    assert result.session.user_id == user.id
    assert result.session.family_id == result.tokens.family_id
    assert result.session.token_hash == result.tokens.refresh_token_hash
    assert uow.commits == 1
    assert uow._fake_users.last_login_updates[user.id] is not None


@pytest.mark.unit
async def test_wrong_password_raises_invalid_credentials_and_no_session() -> None:
    hasher = FakePasswordHasher()
    users = FakeUserRepository()
    await users.add(_make_user("bob@example.com", "correct", hasher))

    uc, uow, _ = _make_use_case(users=users, hasher=hasher)
    with pytest.raises(InvalidCredentialsError):
        await uc.execute(email="bob@example.com", password="WRONG")

    assert uow.commits == 0
    assert uow._fake_users.last_login_updates == {}


@pytest.mark.unit
async def test_unknown_email_burns_a_verify_and_raises() -> None:
    hasher = FakePasswordHasher()
    uc, uow, _ = _make_use_case(hasher=hasher)  # empty user repo

    with pytest.raises(InvalidCredentialsError):
        await uc.execute(email="ghost@example.com", password="whatever")

    # Anti-enumeration: exactly one verify call, against the dummy hash.
    assert len(hasher.verify_calls) == 1
    assert hasher.verify_calls[0] == ("whatever", _DUMMY)
    assert uow.commits == 0


@pytest.mark.unit
async def test_oauth_only_user_burns_a_verify_and_raises() -> None:
    """A user whose ``password_hash`` is NULL (OAuth-only) can't log in via password."""
    hasher = FakePasswordHasher()
    users = FakeUserRepository()
    user = _make_user("oauth@example.com", "unused", hasher)
    # Rewrite the row so password_hash is None (simulating an OAuth-only account).
    import dataclasses

    users._rows[user.id] = dataclasses.replace(user, password_hash=None)

    uc, uow, _ = _make_use_case(users=users, hasher=hasher)
    with pytest.raises(InvalidCredentialsError):
        await uc.execute(email="oauth@example.com", password="anything")

    assert len(hasher.verify_calls) == 1
    assert hasher.verify_calls[0] == ("anything", _DUMMY)
    assert uow.commits == 0


@pytest.mark.unit
async def test_message_is_identical_for_unknown_email_and_wrong_password() -> None:
    """Anti-enumeration: the client message must never differentiate the two paths."""
    hasher = FakePasswordHasher()
    users = FakeUserRepository()
    await users.add(_make_user("known@example.com", "right", hasher))

    uc_known, _, _ = _make_use_case(users=users, hasher=hasher)
    uc_unknown, _, _ = _make_use_case(hasher=hasher)

    with pytest.raises(InvalidCredentialsError) as unknown_exc:
        await uc_unknown.execute(email="nobody@example.com", password="anything")
    with pytest.raises(InvalidCredentialsError) as wrong_pw_exc:
        await uc_known.execute(email="known@example.com", password="WRONG")

    assert str(unknown_exc.value) == str(wrong_pw_exc.value)


@pytest.mark.unit
async def test_new_family_id_per_login() -> None:
    hasher = FakePasswordHasher()
    users = FakeUserRepository()
    await users.add(_make_user("alice@example.com", "pw", hasher))

    uc1, _, _ = _make_use_case(users=users, hasher=hasher)
    uc2, _, _ = _make_use_case(users=users, hasher=hasher)

    r1 = await uc1.execute(email="alice@example.com", password="pw")
    r2 = await uc2.execute(email="alice@example.com", password="pw")

    assert r1.session.family_id != r2.session.family_id
    assert r1.session.id != r2.session.id
