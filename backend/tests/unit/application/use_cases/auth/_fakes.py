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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from app.application.interfaces.clock import IClock
from app.application.interfaces.repositories import (
    IProjectRepository,
    IProjectVersionRepository,
    IRoleRepository,
    ISceneRepository,
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
from app.domain.scenes.scene import Scene
from app.domain.versions.project_version import ProjectVersion, ProjectVersionSummary

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
                "user already exists",
                details={"constraint": "uq_users_tenant_id_email"},
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


def _scene_gap(left: int | None, right: int | None) -> int | None:
    """Mirror of ``scene_repository._gap`` — kept in sync for faithful unit fakes."""
    step = 1000
    if left is None and right is None:
        return step
    if left is None:
        assert right is not None
        candidate = right - step
        if candidate < 1:
            candidate = right // 2
        if candidate < 1 or candidate >= right:
            return None
        return candidate
    if right is None:
        return left + step
    if right - left <= 1:
        return None
    return (left + right) // 2


@dataclass
class FakeSceneRepository(ISceneRepository):
    """In-memory ``ISceneRepository`` for α5c use-case unit tests.

    Models the same observable contract as the real ``SceneRepository``: one
    implicit default storyboard per project (get-or-create), append-at-end
    numbering (``max + 1000``), sparse gap-based reorder with a full 1000-step
    rebalance fallback, project-scoped visibility, and the version-fenced CAS
    (``version`` bumps by exactly 1 on a real content/order change). The fake
    models the post-filter (live-rows-only) view, so a scene present in
    ``_scenes`` is a live scene; soft delete drops it. ``project_id`` scoping
    works via the single ``project → storyboard`` map (``_default_sb``).
    """

    _default_sb: dict[UUID, UUID] = field(default_factory=dict)
    _scenes: dict[UUID, Scene] = field(default_factory=dict)

    async def ensure_default_storyboard(self, project_id: UUID) -> tuple[UUID, bool]:
        existing = self._default_sb.get(project_id)
        if existing is not None:
            return existing, False
        storyboard_id = uuid4()
        self._default_sb[project_id] = storyboard_id
        return storyboard_id, True

    async def add(
        self,
        *,
        storyboard_id: UUID,
        title: str,
        duration_seconds: float,
        narration: str | None,
        subtitle: str | None,
    ) -> Scene:
        live = [s for s in self._scenes.values() if s.storyboard_id == storyboard_id]
        max_num = max((s.scene_number for s in live), default=None)
        next_num = 1000 if max_num is None else max_num + 1000
        now = datetime.now(UTC)
        scene = Scene(
            id=uuid4(),
            storyboard_id=storyboard_id,
            scene_number=next_num,
            title=title,
            duration_seconds=duration_seconds,
            narration=narration,
            subtitle=subtitle,
            created_at=now,
            updated_at=now,
            version=1,
        )
        self._scenes[scene.id] = scene
        return scene

    def _scoped(self, project_id: UUID, scene_id: UUID) -> Scene | None:
        sb = self._default_sb.get(project_id)
        scene = self._scenes.get(scene_id)
        if scene is None or sb is None or scene.storyboard_id != sb:
            return None
        return scene

    async def list_by_project(self, project_id: UUID) -> list[Scene]:
        sb = self._default_sb.get(project_id)
        if sb is None:
            return []
        scenes = [s for s in self._scenes.values() if s.storyboard_id == sb]
        scenes.sort(key=lambda s: s.scene_number)
        return scenes

    async def get_owned_scene(self, project_id: UUID, scene_id: UUID) -> Scene | None:
        return self._scoped(project_id, scene_id)

    async def position_of(self, storyboard_id: UUID, scene_number: int) -> int:
        return (
            sum(
                1
                for s in self._scenes.values()
                if s.storyboard_id == storyboard_id and s.scene_number < scene_number
            )
            + 1
        )

    async def update_owned(
        self,
        project_id: UUID,
        scene_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> Scene | None:
        scene = self._scoped(project_id, scene_id)
        if scene is None or scene.version != expected_version:
            return None
        updated = replace(
            scene,
            **dict(changes),
            version=scene.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._scenes[scene_id] = updated
        return updated

    async def soft_delete_owned(self, project_id: UUID, scene_id: UUID) -> bool:
        scene = self._scoped(project_id, scene_id)
        if scene is None:
            return False
        del self._scenes[scene_id]
        return True

    async def reorder_owned(
        self,
        project_id: UUID,
        scene_id: UUID,
        target_position: int,
        expected_version: int,
    ) -> Scene | None:
        moved = self._scoped(project_id, scene_id)
        if moved is None or moved.version != expected_version:
            return None
        sb = moved.storyboard_id
        ordered = sorted(
            (s for s in self._scenes.values() if s.storyboard_id == sb),
            key=lambda s: s.scene_number,
        )
        ordered_ids = [s.id for s in ordered]
        others = [s for s in ordered if s.id != scene_id]
        n = len(others)
        k = max(0, min(target_position - 1, n))
        if ordered_ids.index(scene_id) == k:
            return moved  # no-op
        left = others[k - 1].scene_number if k > 0 else None
        right = others[k].scene_number if k < n else None
        new_number = _scene_gap(left, right)
        now = datetime.now(UTC)
        if new_number is not None:
            updated = replace(
                moved,
                scene_number=new_number,
                version=moved.version + 1,
                updated_at=now,
            )
            self._scenes[scene_id] = updated
            return updated
        # Rebalance: renumber the whole storyboard to 1000-step slots.
        final_order = others[:k] + [moved] + others[k:]
        result = moved
        for i, s in enumerate(final_order):
            number = (i + 1) * 1000
            updated = replace(s, scene_number=number, version=s.version + 1, updated_at=now)
            self._scenes[s.id] = updated
            if s.id == scene_id:
                result = updated
        return result


@dataclass
class FakeProjectVersionRepository(IProjectVersionRepository):
    """In-memory ``IProjectVersionRepository`` for α5d.1 use-case unit tests.

    Models the same observable contract as the real
    ``ProjectVersionRepository``: monotonic per-project ``version_number``
    (``MAX + 1``), the ``parent_version_id`` lineage link (previous current →
    parent), the ``current_version_id`` pointer advance + project ``version``
    bump on capture (α5d Q6), newest-first metadata listing, and UUID-addressed
    single reads scoped to the project. It reads the SIBLING project + scene
    fakes (wired by ``FakeUnitOfWork``) so a snapshot faithfully reflects the
    project's live scenes in order (snapshot BODY fidelity — fat fields,
    decimal-as-string — is an integration concern; the unit fake carries the
    slim α5c scene view).
    """

    _projects: FakeProjectRepository
    _scenes: FakeSceneRepository
    _versions: dict[UUID, ProjectVersion] = field(default_factory=dict)

    async def create_snapshot(
        self,
        *,
        project_id: UUID,
        created_by_user_id: UUID,
        reason: str,
    ) -> ProjectVersion:
        # The use case gate guarantees the project exists + is owned.
        project = self._projects._rows.get(project_id)
        assert project is not None, "project vanished between ownership gate and capture"

        existing = [v for v in self._versions.values() if v.project_id == project_id]
        next_number = max((v.version_number for v in existing), default=0) + 1
        parent_version_id = project.current_version_id

        scenes = await self._scenes.list_by_project(project_id)  # slim, ordered
        storyboard_id = self._scenes._default_sb.get(project_id)
        snapshot: dict[str, Any] = {
            "schema_version": 1,
            "project": {
                "id": str(project.id),
                "name": project.name,
                "description": project.description,
                "aspect_ratio": project.aspect_ratio,
                "duration_seconds": (
                    None if project.duration_seconds is None else str(project.duration_seconds)
                ),
                "language": project.language,
                "style": project.style,
                "settings": project.settings,
                "version": project.version,
            },
            "storyboard": (None if storyboard_id is None else {"id": str(storyboard_id)}),
            "scenes": [
                {
                    "id": str(s.id),
                    "scene_number": s.scene_number,
                    "title": s.title,
                    "duration_seconds": str(s.duration_seconds),
                    "narration": s.narration,
                    "subtitle": s.subtitle,
                }
                for s in scenes
            ],
        }

        now = datetime.now(UTC)
        version = ProjectVersion(
            id=uuid4(),
            project_id=project_id,
            version_number=next_number,
            parent_version_id=parent_version_id,
            created_by_user_id=created_by_user_id,
            reason=reason,
            snapshot=snapshot,
            diff_summary=None,
            created_at=now,
        )
        self._versions[version.id] = version
        # Advance the current pointer + bump project version (mirrors Q6).
        self._projects._rows[project_id] = replace(
            project,
            current_version_id=version.id,
            version=project.version + 1,
            updated_at=now,
        )
        return version

    async def list_by_project(self, project_id: UUID) -> list[ProjectVersionSummary]:
        versions = [v for v in self._versions.values() if v.project_id == project_id]
        versions.sort(key=lambda v: v.version_number, reverse=True)
        return [
            ProjectVersionSummary(
                id=v.id,
                project_id=v.project_id,
                version_number=v.version_number,
                parent_version_id=v.parent_version_id,
                created_by_user_id=v.created_by_user_id,
                reason=v.reason,
                created_at=v.created_at,
            )
            for v in versions
        ]

    async def get_owned(self, project_id: UUID, version_id: UUID) -> ProjectVersion | None:
        version = self._versions.get(version_id)
        if version is None or version.project_id != project_id:
            return None
        return version


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
        scenes: FakeSceneRepository | None = None,
        versions: FakeProjectVersionRepository | None = None,
    ) -> None:
        self._fake_users = users or FakeUserRepository()
        self._fake_tenants = tenants or FakeTenantRepository()
        self._fake_sessions = sessions or FakeSessionRepository()
        self._fake_roles = roles or FakeRoleRepository()
        self._fake_projects = projects or FakeProjectRepository()
        self._fake_scenes = scenes or FakeSceneRepository()
        # The version fake reads the SAME project + scene fakes so a captured
        # snapshot reflects the live scenes (and the current-pointer advance
        # writes back to the shared project fake).
        self._fake_versions = versions or FakeProjectVersionRepository(
            _projects=self._fake_projects, _scenes=self._fake_scenes
        )
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        self.users = self._fake_users
        self.tenants = self._fake_tenants
        self.sessions = self._fake_sessions
        self.roles = self._fake_roles
        self.projects = self._fake_projects
        self.scenes = self._fake_scenes
        self.versions = self._fake_versions
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
