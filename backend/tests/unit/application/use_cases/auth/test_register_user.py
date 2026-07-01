"""Unit tests for ``RegisterUser`` (Slice α2a)."""

from __future__ import annotations

import pytest

from app.application.use_cases.auth.errors import (
    EmailAlreadyRegisteredError,
    TenantSlugCollisionError,
)
from app.application.use_cases.auth.register_user import RegisterUser

from ._fakes import (
    FakeClock,
    FakePasswordHasher,
    FakeRoleRepository,
    FakeSessionRepository,
    FakeTenantRepository,
    FakeTokenIssuer,
    FakeUnitOfWork,
    FakeUserRepository,
)


def _make_use_case(
    *,
    hasher: FakePasswordHasher | None = None,
    users: FakeUserRepository | None = None,
    tenants: FakeTenantRepository | None = None,
    sessions: FakeSessionRepository | None = None,
    roles: FakeRoleRepository | None = None,
    token_issuer: FakeTokenIssuer | None = None,
    clock: FakeClock | None = None,
) -> tuple[RegisterUser, FakeUnitOfWork, FakePasswordHasher, FakeTokenIssuer]:
    uow = FakeUnitOfWork(users=users, tenants=tenants, sessions=sessions, roles=roles)
    h = hasher or FakePasswordHasher()
    ti = token_issuer or FakeTokenIssuer()
    ck = clock or FakeClock()
    uc = RegisterUser(uow=uow, hasher=h, token_issuer=ti, clock=ck)
    return uc, uow, h, ti


@pytest.mark.unit
async def test_happy_path_creates_tenant_user_session_and_owner_role() -> None:
    uc, uow, hasher, issuer = _make_use_case()

    result = await uc.execute(
        email="alice@example.com",
        password="correct horse battery staple",
        name="Alice",
    )

    # Committed exactly once.
    assert uow.commits == 1
    # Domain entities are populated.
    assert result.user.email == "alice@example.com"
    assert result.user.display_name == "Alice"
    assert result.user.password_hash == "hash::correct horse battery staple"
    assert result.tenant.plan_tier == "free"
    assert result.tenant.slug.startswith("alice-")
    # Single role assignment: 'owner'. See RegisterUser docstring for
    # why 'user' is intentionally NOT assigned (the auth_role ENUM's
    # 'user' code does not exist in the roles lookup table; "any
    # authenticated" is enforced via JWT validity, not a role row).
    assert uow._fake_roles.assignments == [(result.user.id, "owner")]
    # Session persisted with the token issuer's session_id.
    assert result.session.id == result.tokens.session_id
    # Refresh hash matches issuer's SHA-256 output.
    assert result.session.token_hash == result.tokens.refresh_token_hash
    # Issuer was called once, in login mode.
    assert len(issuer.calls) == 1 and issuer.calls[0][0] == "login"


@pytest.mark.unit
async def test_duplicate_email_raises_email_already_registered() -> None:
    users = FakeUserRepository()
    uc, uow, _, _ = _make_use_case(users=users)

    await uc.execute(email="bob@example.com", password="password123", name="Bob")

    with pytest.raises(EmailAlreadyRegisteredError):
        await uc.execute(email="bob@example.com", password="password123", name="Bob")

    # First execute committed; second did not.
    assert uow.commits == 1


@pytest.mark.unit
async def test_duplicate_email_precheck_short_circuits_before_tenant_insert() -> None:
    """The app-level email pre-check must fire BEFORE any tenant is created.

    Under the auto-tenant-per-signup design, the DB per-tenant unique
    constraint on ``(tenant_id, email)`` cannot catch duplicate emails
    across tenants. If the pre-check were skipped (or moved), a second
    registration with the same email would silently produce a second
    orphan tenant. This test locks in the invariant: the second
    registration must not touch the tenants table at all.
    """
    users = FakeUserRepository()
    tenants = FakeTenantRepository()
    uc, uow, _, _ = _make_use_case(users=users, tenants=tenants)

    await uc.execute(email="carol@example.com", password="password123", name="Carol")
    tenants_after_first = len(tenants._rows)

    with pytest.raises(EmailAlreadyRegisteredError):
        await uc.execute(email="carol@example.com", password="password123", name="Carol")

    # No orphan tenant created by the failed re-registration.
    assert len(tenants._rows) == tenants_after_first


@pytest.mark.unit
async def test_tenant_slug_collision_retries_and_eventually_succeeds() -> None:
    tenants = FakeTenantRepository()
    # Pre-poison a slug so the first randomly-generated candidate
    # *might* hit it — actual retry behaviour is that each attempt
    # uses a fresh 6-hex-char suffix, so collision on all 3 is
    # astronomically unlikely. Instead, poison via ``reject_slugs``
    # deterministically: reject the first two arbitrary candidates by
    # rejecting everything shaped like the slug base for 2 tries.
    # Simplest: patch add() to fail the first 2 calls.
    original_add = tenants.add
    call_count = {"n": 0}

    async def flaky_add(t):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] <= 2:
            from app.core.errors import ConflictError

            raise ConflictError("tenant slug already taken", details={"slug": t.slug})
        return await original_add(t)

    tenants.add = flaky_add  # type: ignore[method-assign]

    uc, uow, _, _ = _make_use_case(tenants=tenants)
    result = await uc.execute(email="c@example.com", password="password123", name="Carol")
    assert result.tenant.slug.startswith("carol-")
    assert call_count["n"] == 3  # two failures + one success
    assert uow.commits == 1


@pytest.mark.unit
async def test_tenant_slug_exhaustion_raises() -> None:
    tenants = FakeTenantRepository()

    async def always_fail(t):  # type: ignore[no-untyped-def]
        from app.core.errors import ConflictError

        raise ConflictError("tenant slug already taken", details={"slug": t.slug})

    tenants.add = always_fail  # type: ignore[method-assign]

    uc, uow, _, _ = _make_use_case(tenants=tenants)
    with pytest.raises(TenantSlugCollisionError):
        await uc.execute(email="d@example.com", password="password123", name="Dave")
    assert uow.commits == 0


@pytest.mark.unit
async def test_password_is_hashed_before_persistence() -> None:
    hasher = FakePasswordHasher()
    uc, _, _, _ = _make_use_case(hasher=hasher)

    await uc.execute(email="e@example.com", password="s3cret!!", name="Eve")

    assert hasher.hash_calls == ["s3cret!!"]  # exactly one hash call
    # No verify calls on registration (verify is a login concern).
    assert hasher.verify_calls == []


@pytest.mark.unit
async def test_slug_derived_from_display_name_local_part_falls_back_when_empty() -> None:
    uc, _, _, _ = _make_use_case()
    # A name of ``!!!`` slugifies to empty → "workspace" fallback.
    result = await uc.execute(email="x@example.com", password="password123", name="!!!")
    assert result.tenant.slug.startswith("workspace-")
