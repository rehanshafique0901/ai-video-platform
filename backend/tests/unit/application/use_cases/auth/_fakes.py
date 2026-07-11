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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from app.application.interfaces.clock import IClock
from app.application.interfaces.repositories import (
    IProjectRepository,
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
from app.domain.projects.project import Project

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

    async def update_profile(
        self,
        user_id: UUID,
        expected_version: int,
        display_name: str,
    ) -> User | None:
        # α4 additions — mirrors the concrete ``UserRepository.update_profile``
        # contract: None on version mismatch OR missing user; unchanged
        # entity on same-value no-op; version-bumped entity on real change.
        # The fake stores every user under ``_rows[user_id]`` regardless of
        # soft-delete state (the fake has no ``deleted_at`` concept — it
        # models the post-filter view), so "user gone" here means "not in
        # the dict".
        user = self._rows.get(user_id)
        if user is None or user.version != expected_version:
            return None
        if user.display_name == display_name:
            return user  # same-value no-op — no state change

        import dataclasses

        updated = dataclasses.replace(
            user,
            display_name=display_name,
            version=user.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._rows[user_id] = updated
        return updated


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

    async def get_by_id(self, session_id: UUID) -> Session | None:
        # α3: sid-driven lookup for ``get_current_user``. Returns
        # revoked rows too, matching the real repo's contract.
        return self._rows.get(session_id)

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


@dataclass
class FakeProjectRepository(IProjectRepository):
    """In-memory ``IProjectRepository`` for α5a use-case unit tests.

    Models the same observable contract as the real
    ``ProjectRepository``: the live-row partial-unique index on
    ``(tenant_id, owner_user_id, name)`` (``add`` raises
    ``ConflictError`` on a duplicate), owner-and-tenant scoping on reads,
    and ``created_at DESC, id DESC`` keyset ordering on ``list_owned``.
    The fake has no ``deleted_at`` concept — it models the post-filter
    view, so a row present in ``_rows`` is a live row.
    """

    _rows: dict[UUID, Project] = field(default_factory=dict)

    async def add(self, project: Project) -> Project:
        for existing in self._rows.values():
            if (
                existing.tenant_id == project.tenant_id
                and existing.owner_user_id == project.owner_user_id
                and existing.name == project.name
            ):
                raise ConflictError(
                    "project already exists",
                    details={"constraint": "uq_projects_tenant_id_owner_user_id_name"},
                )
        self._rows[project.id] = project
        return project

    async def get_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> Project | None:
        project = self._rows.get(project_id)
        if (
            project is None
            or project.tenant_id != tenant_id
            or project.owner_user_id != owner_user_id
        ):
            return None
        return project

    async def list_owned(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[Project]:
        scoped = [
            p
            for p in self._rows.values()
            if p.tenant_id == tenant_id and p.owner_user_id == owner_user_id
        ]
        # Newest first; ``id`` breaks ``created_at`` ties (total order).
        scoped.sort(key=lambda p: (p.created_at, p.id), reverse=True)
        if after is not None:
            after_created_at, after_id = after
            scoped = [p for p in scoped if (p.created_at, p.id) < (after_created_at, after_id)]
        return scoped[:limit]

    async def update_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> Project | None:
        # α5b: version-fenced CAS. Mirrors the real repo's observable
        # contract — None when the fenced row is absent / out-of-scope /
        # version-stale (the use case has already established visibility
        # via ``get_owned``, so in the normal flow None here means a
        # concurrent bump → 412). Rename to a name held by ANOTHER live
        # owned row raises ``ConflictError`` (→ 409). ``version`` bumps by
        # exactly 1 (matches the guarded DB trigger's net effect).
        import dataclasses

        row = self._rows.get(project_id)
        if (
            row is None
            or row.tenant_id != tenant_id
            or row.owner_user_id != owner_user_id
            or row.version != expected_version
        ):
            return None
        new_name = changes.get("name", row.name)
        if new_name != row.name:
            for other in self._rows.values():
                if (
                    other.id != project_id
                    and other.tenant_id == tenant_id
                    and other.owner_user_id == owner_user_id
                    and other.name == new_name
                ):
                    raise ConflictError(
                        "project already exists",
                        details={"constraint": "uq_projects_tenant_id_owner_user_id_name"},
                    )
        updated = dataclasses.replace(
            row,
            **dict(changes),
            version=row.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._rows[project_id] = updated
        return updated

    async def soft_delete_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> bool:
        # α5b: owner+tenant-scoped soft delete. The fake models the
        # post-filter (live-rows-only) view, so "soft delete" = drop from
        # ``_rows``; a second call then finds nothing → False → 404 at the
        # use case (idempotent-by-404). No version fence (α5b D8).
        row = self._rows.get(project_id)
        if row is None or row.tenant_id != tenant_id or row.owner_user_id != owner_user_id:
            return False
        del self._rows[project_id]
        return True


# ---- UoW --------------------------------------------------------------


class FakeUnitOfWork(IUnitOfWork):
    """Wraps in-memory fakes into an IUnitOfWork surface. commit() is a bookkeeping flag."""

    def __init__(
        self,
        users: FakeUserRepository | None = None,
        tenants: FakeTenantRepository | None = None,
        sessions: FakeSessionRepository | None = None,
        roles: FakeRoleRepository | None = None,
        projects: FakeProjectRepository | None = None,
    ) -> None:
        self._fake_users = users or FakeUserRepository()
        self._fake_tenants = tenants or FakeTenantRepository()
        self._fake_sessions = sessions or FakeSessionRepository()
        self._fake_roles = roles or FakeRoleRepository()
        self._fake_projects = projects or FakeProjectRepository()
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        self.users = self._fake_users
        self.tenants = self._fake_tenants
        self.sessions = self._fake_sessions
        self.roles = self._fake_roles
        self.projects = self._fake_projects
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
