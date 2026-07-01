"""In-memory fakes for the auth use-case unit tests.

Substituting real Argon2id + SQLAlchemy with these fakes brings a
30-test unit suite from ~10 seconds (dominated by ~300 ms Argon2
verifies) down to well under a second. The port surfaces are small
enough that each fake is < 30 LOC and easy to reason about.

None of these fakes leak into integration tests — those exercise
the real implementations against a live database via the fixtures in
``tests/integration/conftest.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from app.application.interfaces.clock import IClock
from app.application.interfaces.repositories import (
    IRoleRepository,
    ISessionRepository,
    ITenantRepository,
    IUserRepository,
)
from app.application.interfaces.security import (
    IPasswordHasher,
    IssuedTokens,
    ITokenIssuer,
    TokenClaims,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError, NotFoundError
from app.domain.identity.session import Session
from app.domain.identity.tenant import Tenant
from app.domain.identity.user import User

# ---- Password hasher --------------------------------------------------


class FakePasswordHasher(IPasswordHasher):
    """Deterministic, no-crypto hasher: hash("pw") == "hash::pw".

    ``verify`` is O(len(pw)) not O(300 ms) — makes the auth unit suite
    ~500x faster than real Argon2id. Preserves the same behavioural
    contract (verify(pw, hash(pw)) is True; anything else is False;
    verify never raises).
    """

    PREFIX = "hash::"

    def __init__(self) -> None:
        self.hash_calls: list[str] = []
        self.verify_calls: list[tuple[str, str]] = []

    def hash(self, password: str) -> str:
        self.hash_calls.append(password)
        return f"{self.PREFIX}{password}"

    def verify(self, password: str, hashed: str) -> bool:
        self.verify_calls.append((password, hashed))
        return hashed == f"{self.PREFIX}{password}"

    def needs_rehash(self, hashed: str) -> bool:
        return False


# ---- Token issuer -----------------------------------------------------


class FakeTokenIssuer(ITokenIssuer):
    """Emits deterministic-ish tokens; hash is real SHA-256 for realism.

    α2b: the fake also keeps an in-memory registry of every issued
    (token, claims) pair so ``verify_access`` / ``verify_refresh`` can
    round-trip without invoking real JWT parsing. Tests that want to
    simulate a signature-invalid / expired / tampered token simply
    pass a string the fake has never seen — it raises ``UnauthorizedError``
    exactly like the real ``AuthTokenIssuer`` would.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID]] = []  # (mode, family_id)
        # Instance-level (not class-level) so state cannot leak across
        # tests via a shared class dict.
        self._claims_by_token: dict[str, TokenClaims] = {}

    def issue_for_login(self, user: User) -> IssuedTokens:
        return self._issue(user, family_id=uuid4(), mode="login")

    def issue_for_rotation(self, user: User, family_id: UUID) -> IssuedTokens:
        return self._issue(user, family_id, mode="rotation")

    def verify_access(self, token: str, *, allow_expired: bool = False) -> TokenClaims:
        # α2b.2: ``allow_expired`` is accepted for port conformance but
        # has no effect on the fake — the in-memory claim registry has
        # no concept of expiry. Tests that specifically exercise the
        # expired-access-with-logout path use the real ``AuthTokenIssuer``.
        return self._verify(token)

    def verify_refresh(self, token: str) -> TokenClaims:
        return self._verify(token)

    def _verify(self, token: str) -> TokenClaims:
        claims = self._claims_by_token.get(token)
        if claims is None:
            from app.core.errors import UnauthorizedError

            raise UnauthorizedError("invalid token")
        return claims

    def _register(self, tokens: IssuedTokens, subject: UUID) -> None:
        claims = TokenClaims(
            subject=subject,
            session_id=tokens.session_id,
            family_id=tokens.family_id,
            expires_at=tokens.refresh_expires_at,
        )
        self._claims_by_token[tokens.access_token] = claims
        self._claims_by_token[tokens.refresh_token] = claims

    def _issue(self, user: User, family_id: UUID, mode: str) -> IssuedTokens:
        self.calls.append((mode, family_id))
        session_id = uuid4()
        now = datetime.now(UTC)
        access = f"access.{user.id}.{session_id}"
        refresh = f"refresh.{user.id}.{session_id}.{family_id}"
        refresh_hash = hashlib.sha256(refresh.encode()).hexdigest()
        tokens = IssuedTokens(
            access_token=access,
            refresh_token=refresh,
            refresh_token_hash=refresh_hash,
            session_id=session_id,
            family_id=family_id,
            issued_at=now,
            refresh_expires_at=now + timedelta(days=30),
        )
        self._register(tokens, subject=user.id)
        return tokens


# ---- Repositories (in-memory) -----------------------------------------


@dataclass
class FakeUserRepository(IUserRepository):
    _rows: dict[UUID, User] = field(default_factory=dict)
    _by_email: dict[str, UUID] = field(default_factory=dict)
    last_login_updates: dict[UUID, datetime] = field(default_factory=dict)

    async def count(self) -> int:
        return len(self._rows)

    async def exists_by_id(self, user_id: UUID) -> bool:
        return user_id in self._rows

    async def get_by_email(self, email: str) -> User | None:
        uid = self._by_email.get(email)
        return self._rows.get(uid) if uid is not None else None

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._rows.get(user_id)

    async def add(self, user: User) -> User:
        if user.email in self._by_email:
            raise ConflictError(
                "user already exists", details={"constraint": "uq_users_tenant_id_email"}
            )
        self._rows[user.id] = user
        self._by_email[user.email] = user.id
        return user

    async def update_last_login(self, user_id: UUID, at: datetime) -> None:
        self.last_login_updates[user_id] = at


@dataclass
class FakeTenantRepository(ITenantRepository):
    _rows: dict[UUID, Tenant] = field(default_factory=dict)
    _by_slug: dict[str, UUID] = field(default_factory=dict)
    reject_slugs: set[str] = field(default_factory=set)

    async def add(self, tenant: Tenant) -> Tenant:
        if tenant.slug in self._by_slug or tenant.slug in self.reject_slugs:
            raise ConflictError("tenant slug already taken", details={"slug": tenant.slug})
        self._rows[tenant.id] = tenant
        self._by_slug[tenant.slug] = tenant.id
        return tenant

    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:
        return self._rows.get(tenant_id)

    async def exists_by_slug(self, slug: str) -> bool:
        return slug in self._by_slug


@dataclass
class FakeSessionRepository(ISessionRepository):
    _rows: dict[UUID, Session] = field(default_factory=dict)
    revoke_calls: list[UUID] = field(default_factory=list)
    list_family_calls: list[UUID] = field(default_factory=list)

    async def add(self, session: Session) -> Session:
        self._rows[session.id] = session
        return session

    async def get_by_hash(self, token_hash: str) -> Session | None:
        # Linear scan — fine for unit tests (families are small).
        # Returns revoked rows too, matching the real repo's contract
        # (the caller uses revoked_at != None as the reuse signal).
        for row in self._rows.values():
            if row.token_hash == token_hash:
                return row
        return None

    async def revoke(self, session_id: UUID, at: datetime) -> bool:
        self.revoke_calls.append(session_id)
        row = self._rows.get(session_id)
        if row is None or row.revoked_at is not None:
            return False  # CAS loser: unknown row or already revoked
        import dataclasses

        self._rows[session_id] = dataclasses.replace(row, revoked_at=at)
        return True

    async def list_family(self, family_id: UUID) -> list[Session]:
        self.list_family_calls.append(family_id)
        return [row for row in self._rows.values() if row.family_id == family_id]


# ---- Clock ------------------------------------------------------------


@dataclass
class FakeClock(IClock):
    """Freezable clock.

    Default (``fixed_at is None``) returns real wall-clock time — safe
    for tests that don't care about the exact instant. Tests that DO
    care pass a specific ``fixed_at`` and optionally ``tick`` it.
    """

    fixed_at: datetime | None = None

    def now(self) -> datetime:
        if self.fixed_at is not None:
            return self.fixed_at
        return datetime.now(UTC)

    def tick(self, seconds: int) -> None:
        assert self.fixed_at is not None, "cannot tick a clock in wall-clock mode"
        self.fixed_at = self.fixed_at + timedelta(seconds=seconds)


@dataclass
class FakeRoleRepository(IRoleRepository):
    """Mirrors the seed data in ``0002_seed_system_data.py`` — deliberately."""

    _known_codes: set[str] = field(
        default_factory=lambda: {
            # Kept in sync with migration ``0002_seed_system_data`` so a
            # future drift where the use case assigns a role that isn't
            # seeded fails LOUDLY in unit tests too (not only in the
            # integration suite against a real DB).
            "owner",
            "admin",
            "editor",
            "viewer",
            "billing",
            "support",
        }
    )
    assignments: list[tuple[UUID, str]] = field(default_factory=list)

    async def assign_role_by_code(
        self,
        user_id: UUID,
        role_code: str,
        granted_by_user_id: UUID | None = None,
    ) -> None:
        if role_code not in self._known_codes:
            raise NotFoundError(
                f"role {role_code!r} does not exist", details={"role_code": role_code}
            )
        self.assignments.append((user_id, role_code))


# ---- UoW --------------------------------------------------------------


class FakeUnitOfWork(IUnitOfWork):
    """Wraps in-memory fakes into an IUnitOfWork surface. commit() is a bookkeeping flag."""

    def __init__(
        self,
        users: FakeUserRepository | None = None,
        tenants: FakeTenantRepository | None = None,
        sessions: FakeSessionRepository | None = None,
        roles: FakeRoleRepository | None = None,
    ) -> None:
        self._fake_users = users or FakeUserRepository()
        self._fake_tenants = tenants or FakeTenantRepository()
        self._fake_sessions = sessions or FakeSessionRepository()
        self._fake_roles = roles or FakeRoleRepository()
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        self.users = self._fake_users
        self.tenants = self._fake_tenants
        self.sessions = self._fake_sessions
        self.roles = self._fake_roles
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None:
            self.rollbacks += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1
