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
from decimal import Decimal
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from app.application.interfaces.clock import IClock
from app.application.interfaces.locks import IDistributedLockManager, Lease
from app.application.interfaces.publisher import OutboxEvent
from app.application.interfaces.repositories import (
    IEventOutboxRepository,
    IExportJobRepository,
    IMediaRepository,
    IModelPricingRepository,
    INotificationRepository,
    IProjectRepository,
    IProjectVersionRepository,
    IPromptRepository,
    IProviderSettingsRepository,
    IRenderJobRepository,
    IRoleRepository,
    ISceneRepository,
    ISessionRepository,
    ITenantRepository,
    ITimelineRepository,
    IUsageRecordRepository,
    IUserRepository,
    IWorkflowRunRepository,
)
from app.application.interfaces.security import (
    IPasswordHasher,
    IssuedTokens,
    ITokenIssuer,
    TokenClaims,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.interfaces.usage_recorder import (
    DuplicateRequestIdError,
    EffectivePrice,
    NewUsageRecord,
    UsageRecordRow,
)
from app.core.errors import ConflictError, NotFoundError
from app.domain.export.export_job import ExportJob, ExportJobClaim
from app.domain.export.export_status import ExportStatus
from app.domain.identity.session import Session
from app.domain.identity.tenant import Tenant
from app.domain.identity.user import User
from app.domain.media.media_asset import MediaAsset
from app.domain.notifications.notification import Notification
from app.domain.projects.project import Project
from app.domain.prompts.prompt import Prompt
from app.domain.render.render_job import RenderJob
from app.domain.render.render_status import RenderStatus
from app.domain.scenes.scene import Scene
from app.domain.timeline.clip import Clip
from app.domain.timeline.timeline import Timeline
from app.domain.timeline.track import Track
from app.domain.versions.project_version import ProjectVersion, ProjectVersionSummary
from app.domain.workflow.workflow_run import WorkflowCheckpoint, WorkflowRun, WorkflowStep
from app.domain.workflow.workflow_run_status import WorkflowRunStatus
from app.domain.workflow.workflow_step_status import WorkflowStepStatus

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

    async def get_ownership(self, project_id: UUID) -> tuple[UUID, UUID] | None:
        # System-only lookup (α8.4a) — a live row in ``_rows`` is a live project.
        project = self._rows.get(project_id)
        if project is None:
            return None
        return (project.tenant_id, project.owner_user_id)

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

    async def touch_version(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> int | None:
        # Aggregate OCC Rule (α5d.2): advance the project-aggregate OCC token
        # by exactly 1 after a real child mutation. Owner+tenant scoped; None
        # if no live owned row (matches the real repo's RETURNING-None).
        import dataclasses

        row = self._rows.get(project_id)
        if row is None or row.tenant_id != tenant_id or row.owner_user_id != owner_user_id:
            return None
        updated = dataclasses.replace(
            row,
            version=row.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._rows[project_id] = updated
        return updated.version


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

    async def restore(
        self,
        *,
        project_id: UUID,
        source_version_id: UUID,
        restored_by_user_id: UUID,
        expected_project_version: int,
    ) -> ProjectVersion | None:
        # Mirrors the real repo's observable contract: aggregate OCC fence on
        # the project version (stale → None → 412, no writes); reconcile the
        # live scene set to equal the snapshot's (keyed on id, preserving
        # UUIDs); rewrite the mutable project root (aspect_ratio invariant);
        # append a reason=restore version parented on the source; advance the
        # current pointer + bump project version by exactly 1.
        project = self._projects._rows.get(project_id)
        assert project is not None, "project vanished between ownership gate and restore"
        if project.version != expected_project_version:
            return None

        source = self._versions.get(source_version_id)
        assert source is not None and source.project_id == project_id
        snap = source.snapshot
        snap_project = snap["project"]
        snap_scenes = snap.get("scenes", [])
        assert snap_project["aspect_ratio"] == project.aspect_ratio

        # Reconcile the fake's live scene set to the snapshot (by id).
        sb = self._scenes._default_sb.get(project_id)
        if sb is None:
            sb = uuid4()
            self._scenes._default_sb[project_id] = sb
        for sid in [s.id for s in self._scenes._scenes.values() if s.storyboard_id == sb]:
            del self._scenes._scenes[sid]
        now = datetime.now(UTC)
        for s in snap_scenes:
            scene = Scene(
                id=UUID(s["id"]),
                storyboard_id=sb,
                scene_number=s["scene_number"],
                title=s["title"],
                duration_seconds=float(s["duration_seconds"]),
                narration=s.get("narration"),
                subtitle=s.get("subtitle"),
                created_at=now,
                updated_at=now,
                version=1,
            )
            self._scenes._scenes[scene.id] = scene

        new_project_version = expected_project_version + 1
        restored_scenes = await self._scenes.list_by_project(project_id)
        version_id = uuid4()
        restore_snapshot: dict[str, Any] = {
            "schema_version": 1,
            "project": {**snap_project, "version": new_project_version},
            "storyboard": {"id": str(sb), "generated_by": "system"},
            "scenes": [
                {
                    "id": str(sc.id),
                    "scene_number": sc.scene_number,
                    "title": sc.title,
                    "duration_seconds": str(sc.duration_seconds),
                    "narration": sc.narration,
                    "subtitle": sc.subtitle,
                }
                for sc in restored_scenes
            ],
        }
        existing = [v for v in self._versions.values() if v.project_id == project_id]
        next_number = max((v.version_number for v in existing), default=0) + 1
        version = ProjectVersion(
            id=version_id,
            project_id=project_id,
            version_number=next_number,
            parent_version_id=source_version_id,
            created_by_user_id=restored_by_user_id,
            reason="restore",
            snapshot=restore_snapshot,
            diff_summary=None,
            created_at=now,
        )
        self._versions[version_id] = version

        # Rewrite mutable root + advance pointer + bump version by exactly 1.
        self._projects._rows[project_id] = replace(
            project,
            name=snap_project["name"],
            description=snap_project["description"],
            duration_seconds=(
                None
                if snap_project["duration_seconds"] is None
                else float(snap_project["duration_seconds"])
            ),
            language=snap_project["language"],
            style=snap_project["style"],
            settings=snap_project["settings"],
            current_version_id=version_id,
            version=new_project_version,
            updated_at=now,
        )
        return version

    async def branch(
        self,
        *,
        source_project_id: UUID,
        source_version_id: UUID,
        source_version_number: int,
        source_snapshot: dict[str, Any],
        new_project_name: str,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> tuple[Project, ProjectVersion]:
        # Mirrors the real repo's observable contract (α5d.3): fork the source
        # snapshot into a NEW independent project owned by the caller — a live
        # name collision raises ConflictError (→ 409) before any child write;
        # scenes are materialized with FRESH ids under a new default storyboard;
        # v1 is reason=branch with a branched_from provenance block + NULL
        # parent; the new project's current pointer is advanced and its version
        # ends at 2 (created + first capture). The source is never touched.
        for existing in self._projects._rows.values():
            if (
                existing.tenant_id == tenant_id
                and existing.owner_user_id == owner_user_id
                and existing.name == new_project_name
            ):
                raise ConflictError(
                    "project already exists",
                    details={"constraint": "uq_projects_tenant_id_owner_user_id_name"},
                )

        snap_project = source_snapshot["project"]
        snap_scenes = source_snapshot.get("scenes", [])
        now = datetime.now(UTC)

        new_project_id = uuid4()
        new_project = Project(
            id=new_project_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            folder_id=None,
            current_version_id=None,
            name=new_project_name,
            description=snap_project["description"],
            aspect_ratio=snap_project["aspect_ratio"],
            duration_seconds=(
                None
                if snap_project["duration_seconds"] is None
                else float(snap_project["duration_seconds"])
            ),
            language=snap_project["language"],
            style=snap_project["style"],
            settings=snap_project["settings"],
            created_at=now,
            updated_at=now,
            version=1,
        )
        self._projects._rows[new_project_id] = new_project

        sb = uuid4()
        self._scenes._default_sb[new_project_id] = sb
        for s in sorted(snap_scenes, key=lambda d: d["scene_number"]):
            scene = Scene(
                id=uuid4(),  # fresh identity (Q5)
                storyboard_id=sb,
                scene_number=s["scene_number"],
                title=s["title"],
                duration_seconds=float(s["duration_seconds"]),
                narration=s.get("narration"),
                subtitle=s.get("subtitle"),
                created_at=now,
                updated_at=now,
                version=1,
            )
            self._scenes._scenes[scene.id] = scene

        materialized = await self._scenes.list_by_project(new_project_id)
        version_id = uuid4()
        snapshot: dict[str, Any] = {
            "schema_version": 1,
            "project": {
                "id": str(new_project_id),
                "name": new_project.name,
                "description": new_project.description,
                "aspect_ratio": new_project.aspect_ratio,
                "duration_seconds": (
                    None
                    if new_project.duration_seconds is None
                    else str(new_project.duration_seconds)
                ),
                "language": new_project.language,
                "style": new_project.style,
                "settings": new_project.settings,
                "version": new_project.version,
            },
            "storyboard": {"id": str(sb), "generated_by": "system"},
            "scenes": [
                {
                    "id": str(sc.id),
                    "scene_number": sc.scene_number,
                    "title": sc.title,
                    "duration_seconds": str(sc.duration_seconds),
                    "narration": sc.narration,
                    "subtitle": sc.subtitle,
                }
                for sc in materialized
            ],
            "branched_from": {
                "project_id": str(source_project_id),
                "version_id": str(source_version_id),
                "version_number": source_version_number,
            },
        }
        version = ProjectVersion(
            id=version_id,
            project_id=new_project_id,
            version_number=1,
            parent_version_id=None,
            created_by_user_id=owner_user_id,
            reason="branch",
            snapshot=snapshot,
            diff_summary=None,
            created_at=now,
        )
        self._versions[version_id] = version

        # Advance the new project's pointer + bump version → 2 (created + v1).
        self._projects._rows[new_project_id] = replace(
            new_project,
            current_version_id=version_id,
            version=new_project.version + 1,
            updated_at=now,
        )
        return self._projects._rows[new_project_id], version


# ---- Prompt repository ------------------------------------------------


@dataclass
class FakePromptRepository(IPromptRepository):
    """In-memory ``IPromptRepository`` for α6.1 use-case unit tests.

    Models the real ``PromptRepository`` observable contract: project-scoped
    visibility, newest-first listing with ``kind`` / ``scene_id`` filters, and —
    per ADR-0036 — **no** version fence and **no** ``projects.version`` bump on
    any mutation (last-writer-wins). ``_scenes`` is included as an insertion
    ordinal so newest-first is deterministic without real timestamps.
    ``_linkable_models`` is the set of ``ai_models`` ids a test declares
    linkable (:meth:`model_is_linkable` returns membership); a model NOT in the
    set models a missing/retired model → the use case raises ``422``.
    """

    _prompts: dict[UUID, Prompt] = field(default_factory=dict)
    _linkable_models: set[UUID] = field(default_factory=set)
    _order: dict[UUID, int] = field(default_factory=dict)
    _seq: int = 0

    async def add(
        self,
        *,
        project_id: UUID,
        scene_id: UUID | None,
        kind: str,
        text_content: str,
        model_id: UUID | None,
        extra: dict[str, Any],
    ) -> Prompt:
        now = datetime.now(UTC)
        prompt = Prompt(
            id=uuid4(),
            project_id=project_id,
            scene_id=scene_id,
            kind=kind,
            text_content=text_content,
            model_id=model_id,
            extra=dict(extra),
            created_at=now,
            updated_at=now,
        )
        self._prompts[prompt.id] = prompt
        self._seq += 1
        self._order[prompt.id] = self._seq
        return prompt

    async def list_owned(
        self,
        project_id: UUID,
        *,
        kind: str | None = None,
        scene_id: UUID | None = None,
    ) -> list[Prompt]:
        rows = [p for p in self._prompts.values() if p.project_id == project_id]
        if kind is not None:
            rows = [p for p in rows if p.kind == kind]
        if scene_id is not None:
            rows = [p for p in rows if p.scene_id == scene_id]
        # Newest-first: insertion ordinal DESC mirrors (created_at, id) DESC.
        rows.sort(key=lambda p: self._order.get(p.id, 0), reverse=True)
        return rows

    async def get_owned(self, project_id: UUID, prompt_id: UUID) -> Prompt | None:
        prompt = self._prompts.get(prompt_id)
        if prompt is None or prompt.project_id != project_id:
            return None
        return prompt

    async def update_owned(
        self,
        project_id: UUID,
        prompt_id: UUID,
        changes: Mapping[str, Any],
    ) -> Prompt | None:
        prompt = self._prompts.get(prompt_id)
        if prompt is None or prompt.project_id != project_id:
            return None
        # No version fence (ADR-0036): last-writer-wins. updated_at advances.
        updated = replace(prompt, updated_at=datetime.now(UTC), **dict(changes))
        self._prompts[prompt_id] = updated
        return updated

    async def soft_delete_owned(self, project_id: UUID, prompt_id: UUID) -> bool:
        prompt = self._prompts.get(prompt_id)
        if prompt is None or prompt.project_id != project_id:
            return False
        del self._prompts[prompt_id]
        return True

    async def model_is_linkable(self, model_id: UUID) -> bool:
        return model_id in self._linkable_models


# ---- Media repository -------------------------------------------------


@dataclass
class FakeMediaRepository(IMediaRepository):
    """In-memory ``IMediaRepository`` for α6.2 use-case unit tests.

    Models the real ``MediaRepository`` observable contract: **owner-scoped**
    visibility (``tenant_id`` + ``owner_user_id``, NOT project-scoped),
    newest-first listing with ``kind`` / ``source`` / ``project_id`` /
    ``scene_id`` filters, the ``(storage_backend, storage_bucket, storage_key)``
    uniqueness (``add`` raises ``ConflictError`` on a duplicate → 409), and —
    per ADR-0037 — **no** version fence and **no** ``projects.version`` bump on
    any mutation (last-writer-wins). ``_order`` is an insertion ordinal so
    newest-first is deterministic without real timestamps. ``_linkable_models``
    is the set of ``ai_models`` ids a test declares linkable
    (:meth:`model_is_linkable` returns membership); a model NOT in the set
    models a missing/retired model → the use case raises ``422``.
    """

    _media: dict[UUID, MediaAsset] = field(default_factory=dict)
    _linkable_models: set[UUID] = field(default_factory=set)
    _order: dict[UUID, int] = field(default_factory=dict)
    _seq: int = 0

    async def add(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        kind: str,
        source: str,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
        mime_type: str,
        size_bytes: int,
        checksum_sha256: bytes,
        project_id: UUID | None,
        scene_id: UUID | None,
        prompt_id: UUID | None,
        model_id: UUID | None,
        provider: str | None,
        width: int | None,
        height: int | None,
        duration_seconds: float | None,
        source_metadata: dict[str, Any],
    ) -> MediaAsset:
        for existing in self._media.values():
            if (
                existing.storage_backend == storage_backend
                and existing.storage_bucket == storage_bucket
                and existing.storage_key == storage_key
            ):
                raise ConflictError(
                    "media asset already exists for these storage coordinates",
                    details={
                        "constraint": ("uq_media_assets_storage_backend_storage_bucket_storage_key")
                    },
                )
        now = datetime.now(UTC)
        media = MediaAsset(
            id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            kind=kind,
            project_id=project_id,
            scene_id=scene_id,
            prompt_id=prompt_id,
            model_id=model_id,
            provider=provider,
            storage_backend=storage_backend,
            storage_bucket=storage_bucket,
            storage_key=storage_key,
            mime_type=mime_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
            duration_seconds=duration_seconds,
            checksum_sha256=checksum_sha256,
            source=source,
            source_metadata=dict(source_metadata),
            created_at=now,
            updated_at=now,
        )
        self._media[media.id] = media
        self._seq += 1
        self._order[media.id] = self._seq
        return media

    async def list_owned(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        kind: str | None = None,
        source: str | None = None,
        project_id: UUID | None = None,
        scene_id: UUID | None = None,
    ) -> list[MediaAsset]:
        rows = [
            m
            for m in self._media.values()
            if m.tenant_id == tenant_id and m.owner_user_id == owner_user_id
        ]
        if kind is not None:
            rows = [m for m in rows if m.kind == kind]
        if source is not None:
            rows = [m for m in rows if m.source == source]
        if project_id is not None:
            rows = [m for m in rows if m.project_id == project_id]
        if scene_id is not None:
            rows = [m for m in rows if m.scene_id == scene_id]
        # Newest-first: insertion ordinal DESC mirrors (created_at, id) DESC.
        rows.sort(key=lambda m: self._order.get(m.id, 0), reverse=True)
        return rows

    async def get_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> MediaAsset | None:
        media = self._media.get(media_id)
        if media is None or media.tenant_id != tenant_id or media.owner_user_id != owner_user_id:
            return None
        return media

    async def get_by_storage_coords(
        self,
        *,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
    ) -> MediaAsset | None:
        for media in self._media.values():
            if (
                media.storage_backend == storage_backend
                and media.storage_bucket == storage_bucket
                and media.storage_key == storage_key
            ):
                return media
        return None

    async def list_enrichable_generated_videos(
        self, *, target_version: int, limit: int
    ) -> list[MediaAsset]:
        def _version(md: object) -> int:
            if not isinstance(md, dict):
                return 0
            enr = md.get("enrichment")
            if not isinstance(enr, dict):
                return 0
            v = enr.get("version")
            return v if isinstance(v, int) else 0

        candidates = [
            m
            for m in self._media.values()
            if m.kind == "video"
            and m.source == "generated"
            and getattr(m, "deleted_at", None) is None
            # recursion guard (W8.4d.1): derived assets are never enrichment inputs.
            and not (
                isinstance(m.source_metadata, dict) and "parent_media_asset_id" in m.source_metadata
            )
            and _version(m.source_metadata) < target_version
        ]
        candidates.sort(key=lambda m: (m.created_at, m.id))
        return candidates[:limit]

    async def update_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        changes: Mapping[str, Any],
    ) -> MediaAsset | None:
        media = self._media.get(media_id)
        if media is None or media.tenant_id != tenant_id or media.owner_user_id != owner_user_id:
            return None
        # No version fence (ADR-0037): last-writer-wins. updated_at advances.
        updated = replace(media, updated_at=datetime.now(UTC), **dict(changes))
        self._media[media_id] = updated
        return updated

    async def soft_delete_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> bool:
        media = self._media.get(media_id)
        if media is None or media.tenant_id != tenant_id or media.owner_user_id != owner_user_id:
            return False
        del self._media[media_id]
        return True

    async def model_is_linkable(self, model_id: UUID) -> bool:
        return model_id in self._linkable_models


# ---- Timeline repository ----------------------------------------------


@dataclass
class FakeTimelineRepository(ITimelineRepository):
    """In-memory ``ITimelineRepository`` for α6.3a use-case unit tests.

    Models the real ``TimelineRepository`` observable contract: one live timeline
    per project (``add`` raises ``ConflictError`` on a second), the version-fenced
    root CAS (``update_owned`` bumps ``version`` by exactly 1 on a real change),
    the single-token aggregate roll-up (``bump_version`` — fenced when
    ``expected_version`` is given, unconditional when ``None``), z_index
    uniqueness per live timeline (``add_track`` / ``update_track`` raise
    ``ConflictError`` → 409), and timeline-scoped track visibility ordered by
    ``z_index``. The fake models the post-filter (live-rows-only) view: a row
    present in ``_timelines`` / ``_tracks`` is live; soft delete drops it. No row
    ever bumps ``projects.version`` (ADR-0035/ADR-0038).
    """

    _timelines: dict[UUID, Timeline] = field(default_factory=dict)
    _tracks: dict[UUID, Track] = field(default_factory=dict)
    _clips: dict[UUID, Clip] = field(default_factory=dict)

    # ---- timeline root ----

    async def add(
        self,
        *,
        project_id: UUID,
        aspect_ratio: str,
        frame_rate: int,
        background_color: str,
    ) -> Timeline:
        for existing in self._timelines.values():
            if existing.project_id == project_id:
                raise ConflictError(
                    "project already has a timeline",
                    details={"constraint": "uq_timelines_project_id"},
                )
        now = datetime.now(UTC)
        timeline = Timeline(
            id=uuid4(),
            project_id=project_id,
            project_version_id=None,
            duration_seconds=0.0,
            aspect_ratio=aspect_ratio,
            frame_rate=frame_rate,
            background_color=background_color,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._timelines[timeline.id] = timeline
        return timeline

    async def get_by_project(self, project_id: UUID) -> Timeline | None:
        for timeline in self._timelines.values():
            if timeline.project_id == project_id:
                return timeline
        return None

    async def update_owned(
        self,
        project_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> Timeline | None:
        timeline = await self.get_by_project(project_id)
        if timeline is None or timeline.version != expected_version:
            return None
        updated = replace(
            timeline,
            **dict(changes),
            version=timeline.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._timelines[timeline.id] = updated
        return updated

    async def bump_version(
        self,
        project_id: UUID,
        expected_version: int | None,
    ) -> int | None:
        timeline = await self.get_by_project(project_id)
        if timeline is None:
            return None
        if expected_version is not None and timeline.version != expected_version:
            return None
        updated = replace(
            timeline,
            version=timeline.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._timelines[timeline.id] = updated
        return updated.version

    # ---- tracks ----

    def _z_index_taken(
        self, timeline_id: UUID, z_index: int, *, exclude: UUID | None = None
    ) -> bool:
        return any(
            t.timeline_id == timeline_id and t.z_index == z_index and t.id != exclude
            for t in self._tracks.values()
        )

    async def add_track(
        self,
        *,
        timeline_id: UUID,
        kind: str,
        z_index: int,
        name: str,
        locked: bool,
        muted: bool,
    ) -> Track:
        if self._z_index_taken(timeline_id, z_index):
            raise ConflictError(
                "track z_index already in use for this timeline",
                details={"constraint": "uq_tracks_timeline_id_z_index"},
            )
        now = datetime.now(UTC)
        track = Track(
            id=uuid4(),
            timeline_id=timeline_id,
            kind=kind,
            z_index=z_index,
            locked=locked,
            muted=muted,
            name=name,
            created_at=now,
            updated_at=now,
        )
        self._tracks[track.id] = track
        return track

    async def list_tracks(self, timeline_id: UUID) -> list[Track]:
        rows = [t for t in self._tracks.values() if t.timeline_id == timeline_id]
        rows.sort(key=lambda t: t.z_index)
        return rows

    async def get_track(self, timeline_id: UUID, track_id: UUID) -> Track | None:
        track = self._tracks.get(track_id)
        if track is None or track.timeline_id != timeline_id:
            return None
        return track

    async def update_track(
        self,
        timeline_id: UUID,
        track_id: UUID,
        changes: Mapping[str, Any],
    ) -> Track | None:
        track = await self.get_track(timeline_id, track_id)
        if track is None:
            return None
        new_z = changes.get("z_index", track.z_index)
        if new_z != track.z_index and self._z_index_taken(timeline_id, new_z, exclude=track_id):
            raise ConflictError(
                "track z_index already in use for this timeline",
                details={"constraint": "uq_tracks_timeline_id_z_index"},
            )
        updated = replace(track, updated_at=datetime.now(UTC), **dict(changes))
        self._tracks[track_id] = updated
        return updated

    async def soft_delete_track(self, timeline_id: UUID, track_id: UUID) -> bool:
        track = self._tracks.get(track_id)
        if track is None or track.timeline_id != timeline_id:
            return False
        del self._tracks[track_id]
        return True

    # ---- clips ----

    async def add_clip(
        self,
        *,
        track_id: UUID,
        media_asset_id: UUID | None,
        start_seconds: float,
        end_seconds: float,
        source_start_seconds: float,
        source_end_seconds: float,
        volume: float,
        locked: bool,
    ) -> Clip:
        now = datetime.now(UTC)
        clip = Clip(
            id=uuid4(),
            track_id=track_id,
            media_asset_id=media_asset_id,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            source_start_seconds=source_start_seconds,
            source_end_seconds=source_end_seconds,
            volume=volume,
            locked=locked,
            transition_in_id=None,
            transition_out_id=None,
            effects=[],
            created_at=now,
            updated_at=now,
        )
        self._clips[clip.id] = clip
        return clip

    def _clip_sort_key(self, clip: Clip) -> tuple[float, UUID]:
        return (clip.start_seconds, clip.id)

    async def list_clips(self, track_id: UUID) -> list[Clip]:
        rows = [c for c in self._clips.values() if c.track_id == track_id]
        rows.sort(key=self._clip_sort_key)
        return rows

    async def list_clips_for_timeline(self, timeline_id: UUID) -> dict[UUID, list[Clip]]:
        live_track_ids = {t.id for t in self._tracks.values() if t.timeline_id == timeline_id}
        grouped: dict[UUID, list[Clip]] = {}
        for clip in self._clips.values():
            if clip.track_id in live_track_ids:
                grouped.setdefault(clip.track_id, []).append(clip)
        for clips in grouped.values():
            clips.sort(key=self._clip_sort_key)
        return grouped

    async def get_clip(self, track_id: UUID, clip_id: UUID) -> Clip | None:
        clip = self._clips.get(clip_id)
        if clip is None or clip.track_id != track_id:
            return None
        return clip

    async def update_clip(
        self,
        track_id: UUID,
        clip_id: UUID,
        changes: Mapping[str, Any],
    ) -> Clip | None:
        clip = await self.get_clip(track_id, clip_id)
        if clip is None:
            return None
        updated = replace(clip, updated_at=datetime.now(UTC), **dict(changes))
        self._clips[clip_id] = updated
        return updated

    async def soft_delete_clip(self, track_id: UUID, clip_id: UUID) -> bool:
        clip = self._clips.get(clip_id)
        if clip is None or clip.track_id != track_id:
            return False
        del self._clips[clip_id]
        return True


# ---- Render-job repository --------------------------------------------


@dataclass
class FakeRenderJobRepository(IRenderJobRepository):
    """In-memory ``IRenderJobRepository`` for α7.1 use-case unit tests.

    Models the real ``RenderJobRepository`` observable contract: **project-scoped**
    visibility (no owner columns), the ``(project_id, idempotency_key)`` uniqueness
    (``add`` raises ``ConflictError`` on a duplicate), newest-first listing with a
    ``status`` filter, and the **self-versioned** cancel CAS (``cancel`` fences on
    the job's own ``version`` AND the ``queued``/``running`` terminal guard,
    bumping ``version`` by exactly 1). ``_order`` is an insertion ordinal so
    newest-first is deterministic without real timestamps. No ``deleted_at``
    concept (render jobs are not soft-deleted).
    """

    _jobs: dict[UUID, RenderJob] = field(default_factory=dict)
    _order: dict[UUID, int] = field(default_factory=dict)
    _seq: int = 0

    async def add(
        self,
        *,
        project_id: UUID,
        timeline_id: UUID,
        pipeline: str,
        pipeline_version: str,
        queue: str,
        priority: int,
        status: str,
        idempotency_key: str | None,
    ) -> RenderJob:
        if idempotency_key is not None:
            for existing in self._jobs.values():
                if (
                    existing.project_id == project_id
                    and existing.idempotency_key == idempotency_key
                ):
                    raise ConflictError(
                        "render job already exists for this idempotency key",
                        details={"constraint": "uq_render_jobs_project_id_idempotency_key"},
                    )
        now = datetime.now(UTC)
        job = RenderJob(
            id=uuid4(),
            project_id=project_id,
            timeline_id=timeline_id,
            workflow_run_id=None,
            pipeline=pipeline,
            pipeline_version=pipeline_version,
            queue=queue,
            priority=priority,
            status=status,
            started_at=None,
            finished_at=None,
            progress="0.00",
            error=None,
            output_media_asset_id=None,
            idempotency_key=idempotency_key,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._jobs[job.id] = job
        self._seq += 1
        self._order[job.id] = self._seq
        return job

    async def get_by_project_and_key(
        self, project_id: UUID, idempotency_key: str
    ) -> RenderJob | None:
        for job in self._jobs.values():
            if job.project_id == project_id and job.idempotency_key == idempotency_key:
                return job
        return None

    async def list_by_project(
        self,
        project_id: UUID,
        *,
        status: str | None = None,
    ) -> list[RenderJob]:
        rows = [j for j in self._jobs.values() if j.project_id == project_id]
        if status is not None:
            rows = [j for j in rows if j.status == status]
        rows.sort(key=lambda j: self._order.get(j.id, 0), reverse=True)
        return rows

    async def get_owned(self, project_id: UUID, render_job_id: UUID) -> RenderJob | None:
        job = self._jobs.get(render_job_id)
        if job is None or job.project_id != project_id:
            return None
        return job

    async def cancel(
        self,
        project_id: UUID,
        render_job_id: UUID,
        expected_version: int,
    ) -> RenderJob | None:
        job = self._jobs.get(render_job_id)
        if job is None or job.project_id != project_id:
            return None
        # Version fence + terminal-state guard (queued/running only), mirroring
        # the real CAS predicate.
        if job.version != expected_version or not RenderStatus(job.status).is_cancelable:
            return None
        updated = replace(
            job,
            status=RenderStatus.CANCELED.value,
            version=job.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._jobs[render_job_id] = updated
        return updated

    # ---- worker-facing lifecycle transitions (α8.4b) -------------------

    async def list_claimable(self, *, limit: int) -> list[RenderJob]:
        rows = [j for j in self._jobs.values() if j.status == RenderStatus.QUEUED.value]
        # FIFO: insertion ordinal ASC mirrors (created_at, id) ASC.
        rows.sort(key=lambda j: self._order.get(j.id, 0))
        return rows[:limit]

    async def mark_running(self, render_job_id: UUID) -> RenderJob | None:
        job = self._jobs.get(render_job_id)
        if job is None or job.status != RenderStatus.QUEUED.value:
            return None
        updated = replace(
            job,
            status=RenderStatus.RUNNING.value,
            started_at=datetime.now(UTC),
            version=job.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._jobs[render_job_id] = updated
        return updated

    async def mark_succeeded(
        self,
        render_job_id: UUID,
        *,
        output_media_asset_id: UUID,
        progress: str = "100.00",
    ) -> RenderJob | None:
        job = self._jobs.get(render_job_id)
        if job is None or job.status != RenderStatus.RUNNING.value:
            return None
        updated = replace(
            job,
            status=RenderStatus.SUCCEEDED.value,
            finished_at=datetime.now(UTC),
            output_media_asset_id=output_media_asset_id,
            progress=progress,
            version=job.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._jobs[render_job_id] = updated
        return updated

    async def mark_failed(
        self,
        render_job_id: UUID,
        *,
        error: dict[str, object],
    ) -> RenderJob | None:
        job = self._jobs.get(render_job_id)
        if job is None or job.status != RenderStatus.RUNNING.value:
            return None
        updated = replace(
            job,
            status=RenderStatus.FAILED.value,
            finished_at=datetime.now(UTC),
            error=dict(error),
            version=job.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._jobs[render_job_id] = updated
        return updated


# ---- Export-job repository --------------------------------------------


_EXPORT_ACTIVE_STATUSES = (
    ExportStatus.QUEUED.value,
    ExportStatus.RUNNING.value,
    ExportStatus.SUCCEEDED.value,
)


@dataclass
class FakeExportJobRepository(IExportJobRepository):
    """In-memory ``IExportJobRepository`` for α8.5a use-case unit tests.

    Models the real adapter's observable contract: the partial-unique tuple
    ``(render_job_id, format, quality, orientation)`` over active/fulfilled statuses
    (``add`` raises ``ConflictError`` on a duplicate), render-derived ownership (resolved
    through the shared ``FakeRenderJobRepository`` → ``project_id``), FIFO claim scan, and
    the self-versioned worker CAS transitions (hand-set ``version + 1``).
    """

    _render_jobs: FakeRenderJobRepository = field(default_factory=FakeRenderJobRepository)
    _jobs: dict[UUID, ExportJob] = field(default_factory=dict)
    _order: dict[UUID, int] = field(default_factory=dict)
    _seq: int = 0

    def _project_id_of(self, render_job_id: UUID) -> UUID | None:
        render_job = self._render_jobs._jobs.get(render_job_id)
        return render_job.project_id if render_job is not None else None

    async def add(
        self,
        *,
        render_job_id: UUID,
        requested_by_user_id: UUID,
        format: str,
        quality: str,
        orientation: str,
        status: str,
    ) -> ExportJob:
        for existing in self._jobs.values():
            if (
                existing.render_job_id == render_job_id
                and existing.format == format
                and existing.quality == quality
                and existing.orientation == orientation
                and existing.status in _EXPORT_ACTIVE_STATUSES
            ):
                raise ConflictError(
                    "an active export already exists for this render + encoding",
                    details={
                        "constraint": "uq_export_jobs_render_job_id_format_quality_orientation"
                    },
                )
        now = datetime.now(UTC)
        job = ExportJob(
            id=uuid4(),
            render_job_id=render_job_id,
            requested_by_user_id=requested_by_user_id,
            format=format,
            quality=quality,
            orientation=orientation,
            status=status,
            output_media_asset_id=None,
            download_count=0,
            last_downloaded_at=None,
            file_size_bytes=None,
            finished_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._jobs[job.id] = job
        self._seq += 1
        self._order[job.id] = self._seq
        return job

    async def get_active(
        self,
        render_job_id: UUID,
        *,
        format: str,
        quality: str,
        orientation: str,
    ) -> ExportJob | None:
        for job in self._jobs.values():
            if (
                job.render_job_id == render_job_id
                and job.format == format
                and job.quality == quality
                and job.orientation == orientation
                and job.status in _EXPORT_ACTIVE_STATUSES
            ):
                return job
        return None

    async def get_owned(self, project_id: UUID, export_job_id: UUID) -> ExportJob | None:
        job = self._jobs.get(export_job_id)
        if job is None or self._project_id_of(job.render_job_id) != project_id:
            return None
        return job

    async def list_claimable(self, *, limit: int) -> list[ExportJobClaim]:
        rows = [j for j in self._jobs.values() if j.status == ExportStatus.QUEUED.value]
        rows.sort(key=lambda j: self._order.get(j.id, 0))
        claims: list[ExportJobClaim] = []
        for job in rows[:limit]:
            project_id = self._project_id_of(job.render_job_id)
            if project_id is None:  # pragma: no cover — a claimable job always has a render
                continue
            claims.append(ExportJobClaim(export_job_id=job.id, project_id=project_id))
        return claims

    async def mark_running(self, export_job_id: UUID) -> ExportJob | None:
        job = self._jobs.get(export_job_id)
        if job is None or job.status != ExportStatus.QUEUED.value:
            return None
        updated = replace(
            job,
            status=ExportStatus.RUNNING.value,
            version=job.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._jobs[export_job_id] = updated
        return updated

    async def mark_succeeded(
        self,
        export_job_id: UUID,
        *,
        output_media_asset_id: UUID,
        file_size_bytes: int,
    ) -> ExportJob | None:
        job = self._jobs.get(export_job_id)
        if job is None or job.status != ExportStatus.RUNNING.value:
            return None
        updated = replace(
            job,
            status=ExportStatus.SUCCEEDED.value,
            finished_at=datetime.now(UTC),
            output_media_asset_id=output_media_asset_id,
            file_size_bytes=file_size_bytes,
            version=job.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._jobs[export_job_id] = updated
        return updated

    async def mark_failed(self, export_job_id: UUID) -> ExportJob | None:
        job = self._jobs.get(export_job_id)
        if job is None or job.status != ExportStatus.RUNNING.value:
            return None
        updated = replace(
            job,
            status=ExportStatus.FAILED.value,
            finished_at=datetime.now(UTC),
            version=job.version + 1,
            updated_at=datetime.now(UTC),
        )
        self._jobs[export_job_id] = updated
        return updated

    async def record_download(self, export_job_id: UUID) -> ExportJob | None:
        # Telemetry only — no version bump (W8.5b.3); guarded on 'succeeded'.
        job = self._jobs.get(export_job_id)
        if job is None or job.status != ExportStatus.SUCCEEDED.value:
            return None
        updated = replace(
            job,
            download_count=job.download_count + 1,
            last_downloaded_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._jobs[export_job_id] = updated
        return updated


# ---- Notification repository (α8.5b.3) --------------------------------


@dataclass
class FakeNotificationRepository(INotificationRepository):
    """In-memory ``INotificationRepository`` for the notification unit tests (α8.5b.3/3r).

    Models the real adapter's observable contract:
    * **Write (α8.5b.3):** the partial-unique ``(user_id, source_event_id)`` dedupe key
      (``add`` raises ``ConflictError`` on a duplicate where ``source_event_id`` is not
      None), and stamps ``delivered_in_app_at`` at insert. Multiple ``source_event_id IS
      NULL`` rows are permitted (partial index).
    * **Read (α8.5b.3r):** owner-scoped list (keyset, ``created_at DESC, id DESC``, archived
      excluded — W8.5b.8/10), unread count (``read_at IS NULL AND archived = false``),
      scoped idempotent ``mark_read`` (metadata-only — W8.5b.9), and bulk ``mark_all_read``.
    """

    _rows: dict[UUID, Notification] = field(default_factory=dict)

    async def add(
        self,
        *,
        user_id: UUID,
        kind: str,
        title: str,
        body: str | None,
        payload: dict[str, Any],
        source_event_id: UUID | None,
    ) -> Notification:
        if source_event_id is not None:
            for existing in self._rows.values():
                if existing.user_id == user_id and existing.source_event_id == source_event_id:
                    raise ConflictError(
                        "notification already exists for this recipient + source event",
                        details={"constraint": "uq_notifications_user_id_source_event_id"},
                    )
        now = datetime.now(UTC)
        notification = Notification(
            id=uuid4(),
            user_id=user_id,
            kind=kind,
            title=title,
            body=body,
            payload=dict(payload),
            source_event_id=source_event_id,
            delivered_in_app_at=now,
            read_at=None,
            archived=False,
            created_at=now,
            updated_at=now,
        )
        self._rows[notification.id] = notification
        return notification

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[Notification]:
        rows = [n for n in self._rows.values() if n.user_id == user_id and not n.archived]
        # Newest first, total order on (created_at, id) — W8.5b.10 (read_at ignored).
        rows.sort(key=lambda n: (n.created_at, n.id), reverse=True)
        if after is not None:
            rows = [n for n in rows if (n.created_at, n.id) < after]
        return rows[:limit]

    async def count_unread(self, user_id: UUID) -> int:
        return sum(
            1
            for n in self._rows.values()
            if n.user_id == user_id and n.read_at is None and not n.archived
        )

    async def mark_read(self, user_id: UUID, notification_id: UUID) -> Notification | None:
        existing = self._rows.get(notification_id)
        if existing is None or existing.user_id != user_id:
            return None
        if existing.read_at is not None:
            # Already read — idempotent no-op, return unchanged (200 upstream).
            return existing
        updated = replace(existing, read_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        self._rows[notification_id] = updated
        return updated

    async def mark_all_read(self, user_id: UUID) -> int:
        affected = 0
        for nid, n in list(self._rows.items()):
            if n.user_id == user_id and n.read_at is None and not n.archived:
                now = datetime.now(UTC)
                self._rows[nid] = replace(n, read_at=now, updated_at=now)
                affected += 1
        return affected


# ---- Event outbox repository ------------------------------------------


class _FakeOutboxRow:
    """Mutable in-memory ``event_outbox`` row backing the α7.3 relay fake surface."""

    def __init__(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: datetime,
        event_version: str,
        metadata: dict[str, Any],
    ) -> None:
        self.id = uuid4()
        self.aggregate_type = aggregate_type
        self.aggregate_id = aggregate_id
        self.event_type = event_type
        self.payload = payload
        self.occurred_at = occurred_at
        self.event_version = event_version
        self.metadata = metadata
        self.published_at: datetime | None = None
        self.attempts = 0
        self.last_error: str | None = None

    def to_event(self) -> OutboxEvent:
        return OutboxEvent(
            id=self.id,
            aggregate_type=self.aggregate_type,
            aggregate_id=self.aggregate_id,
            event_type=self.event_type,
            event_version=self.event_version,
            payload=dict(self.payload),
            metadata=dict(self.metadata),
            occurred_at=self.occurred_at,
            attempts=self.attempts,
        )


@dataclass
class FakeEventOutboxRepository(IEventOutboxRepository):
    """In-memory ``IEventOutboxRepository`` — records events + models the relay surface.

    Each ``add`` appends a dict of the arguments to :attr:`events` (so producers'
    unit tests assert the aggregate type / event type / payload shape, blueprint §6
    / D9) AND stores a mutable :class:`_FakeOutboxRow` in :attr:`rows`. The α7.3
    relay surface (:meth:`fetch_unpublished` / :meth:`mark_published` /
    :meth:`mark_failed`) operates on ``rows`` so ``RelayService`` can be unit-tested
    without a database: ``fetch_unpublished`` honours the ``published_at IS NULL``
    + ``attempts < max_attempts`` filter and ``occurred_at``/``id`` ordering that
    the real query enforces (there is no cross-transaction row-locking to model
    in-process).
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    rows: list[_FakeOutboxRow] = field(default_factory=list)

    async def add(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: datetime,
        event_version: str = "1.0",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        meta = metadata if metadata is not None else {}
        self.events.append(
            {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "payload": payload,
                "occurred_at": occurred_at,
                "event_version": event_version,
                "metadata": meta,
            }
        )
        self.rows.append(
            _FakeOutboxRow(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload=payload,
                occurred_at=occurred_at,
                event_version=event_version,
                metadata=meta,
            )
        )

    async def fetch_unpublished(self, *, limit: int, max_attempts: int) -> list[OutboxEvent]:
        pending = [r for r in self.rows if r.published_at is None and r.attempts < max_attempts]
        pending.sort(key=lambda r: (r.occurred_at, str(r.id)))
        return [r.to_event() for r in pending[:limit]]

    async def mark_published(self, *, event_id: UUID, published_at: datetime) -> None:
        for r in self.rows:
            if r.id == event_id:
                r.published_at = published_at
                return

    async def mark_failed(self, *, event_id: UUID, error: str) -> None:
        for r in self.rows:
            if r.id == event_id:
                r.attempts += 1
                r.last_error = error
                return


# ---- Workflow-run repository ------------------------------------------


@dataclass
class FakeWorkflowRunRepository(IWorkflowRunRepository):
    """In-memory ``IWorkflowRunRepository`` for α7.2 use-case unit tests.

    Models the real ``WorkflowRunRepository`` observable contract: **project-scoped**
    visibility (no owner columns), the ``(project_id, idempotency_key)`` uniqueness
    (``add`` raises ``ConflictError`` on a duplicate), newest-first listing with a
    ``status`` filter, and the **status-guarded CAS** transitions (no ``version``
    token — a transition returns the row only when the current status is in the
    allowed set, else ``None``). Steps are seeded ``pending``; checkpoints are
    append-only (a plain list, never mutated). ``_order`` is an insertion ordinal so
    newest-first is deterministic without real timestamps.
    """

    _runs: dict[UUID, WorkflowRun] = field(default_factory=dict)
    _steps: dict[UUID, WorkflowStep] = field(default_factory=dict)
    _checkpoints: list[WorkflowCheckpoint] = field(default_factory=list)
    _order: dict[UUID, int] = field(default_factory=dict)
    _seq: int = 0
    _ckpt_seq: int = 0

    # ---- create + seed ----

    async def add(
        self,
        *,
        project_id: UUID,
        workflow_key: str,
        workflow_version: str,
        status: str,
        input_snapshot: dict[str, Any],
        triggered_by_user_id: UUID | None,
        idempotency_key: str | None,
    ) -> WorkflowRun:
        if idempotency_key is not None:
            for existing in self._runs.values():
                if (
                    existing.project_id == project_id
                    and existing.idempotency_key == idempotency_key
                ):
                    raise ConflictError(
                        "workflow run already exists for this idempotency key",
                        details={"constraint": "uq_workflow_runs_project_id_idempotency_key"},
                    )
        now = datetime.now(UTC)
        run = WorkflowRun(
            id=uuid4(),
            project_id=project_id,
            workflow_key=workflow_key,
            workflow_version=workflow_version,
            status=status,
            started_at=None,
            finished_at=None,
            triggered_by_user_id=triggered_by_user_id,
            idempotency_key=idempotency_key,
            input_snapshot=dict(input_snapshot),
            output_summary=None,
            error=None,
            created_at=now,
            updated_at=now,
        )
        self._runs[run.id] = run
        self._seq += 1
        self._order[run.id] = self._seq
        return run

    async def seed_steps(
        self, workflow_run_id: UUID, steps: list[tuple[int, str]]
    ) -> list[WorkflowStep]:
        now = datetime.now(UTC)
        created: list[WorkflowStep] = []
        for index, name in steps:
            step = WorkflowStep(
                id=uuid4(),
                workflow_run_id=workflow_run_id,
                step_index=index,
                step_name=name,
                status=WorkflowStepStatus.PENDING.value,
                started_at=None,
                finished_at=None,
                retries=0,
                input=None,
                output=None,
                error=None,
                created_at=now,
                updated_at=now,
            )
            self._steps[step.id] = step
            created.append(step)
        return sorted(created, key=lambda s: s.step_index)

    # ---- reads ----

    async def get_by_project_and_key(
        self, project_id: UUID, idempotency_key: str
    ) -> WorkflowRun | None:
        for run in self._runs.values():
            if run.project_id == project_id and run.idempotency_key == idempotency_key:
                return run
        return None

    async def list_by_project(
        self, project_id: UUID, *, status: str | None = None
    ) -> list[WorkflowRun]:
        rows = [r for r in self._runs.values() if r.project_id == project_id]
        if status is not None:
            rows = [r for r in rows if r.status == status]
        rows.sort(key=lambda r: self._order.get(r.id, 0), reverse=True)
        return rows

    async def get_owned(self, project_id: UUID, workflow_run_id: UUID) -> WorkflowRun | None:
        run = self._runs.get(workflow_run_id)
        if run is None or run.project_id != project_id:
            return None
        return run

    def _find_step(self, workflow_run_id: UUID, step_index: int) -> WorkflowStep | None:
        for step in self._steps.values():
            if step.workflow_run_id == workflow_run_id and step.step_index == step_index:
                return step
        return None

    async def list_steps(self, workflow_run_id: UUID) -> list[WorkflowStep]:
        rows = [s for s in self._steps.values() if s.workflow_run_id == workflow_run_id]
        rows.sort(key=lambda s: s.step_index)
        return rows

    async def latest_checkpoint(
        self, workflow_run_id: UUID, step_index: int | None = None
    ) -> WorkflowCheckpoint | None:
        matches = [c for c in self._checkpoints if c.workflow_run_id == workflow_run_id]
        if step_index is not None:
            matches = [c for c in matches if c.step_index == step_index]
        return matches[-1] if matches else None

    # ---- run transitions (status-guarded CAS) ----

    def _cas_run(
        self, workflow_run_id: UUID, allowed: set[str], **changes: Any
    ) -> WorkflowRun | None:
        run = self._runs.get(workflow_run_id)
        if run is None or run.status not in allowed:
            return None
        updated = replace(run, updated_at=datetime.now(UTC), **changes)
        self._runs[workflow_run_id] = updated
        return updated

    async def mark_run_running(self, workflow_run_id: UUID) -> WorkflowRun | None:
        return self._cas_run(
            workflow_run_id,
            {WorkflowRunStatus.QUEUED.value},
            status=WorkflowRunStatus.RUNNING.value,
            started_at=datetime.now(UTC),
        )

    async def mark_run_succeeded(
        self, workflow_run_id: UUID, output_summary: dict[str, Any]
    ) -> WorkflowRun | None:
        return self._cas_run(
            workflow_run_id,
            {WorkflowRunStatus.RUNNING.value},
            status=WorkflowRunStatus.SUCCEEDED.value,
            output_summary=dict(output_summary),
            finished_at=datetime.now(UTC),
        )

    async def mark_run_failed(
        self, workflow_run_id: UUID, error: dict[str, Any]
    ) -> WorkflowRun | None:
        return self._cas_run(
            workflow_run_id,
            {WorkflowRunStatus.RUNNING.value},
            status=WorkflowRunStatus.FAILED.value,
            error=dict(error),
            finished_at=datetime.now(UTC),
        )

    async def mark_run_paused(self, workflow_run_id: UUID) -> WorkflowRun | None:
        # CAS ``running → paused`` (α7.6). ``paused`` is not terminal → no ``finished_at``.
        return self._cas_run(
            workflow_run_id,
            {WorkflowRunStatus.RUNNING.value},
            status=WorkflowRunStatus.PAUSED.value,
        )

    async def resume_run(self, workflow_run_id: UUID) -> WorkflowRun | None:
        # CAS ``paused → running`` (α8.3). Inverse of pause; no ``finished_at``.
        return self._cas_run(
            workflow_run_id,
            {WorkflowRunStatus.PAUSED.value},
            status=WorkflowRunStatus.RUNNING.value,
        )

    async def list_paused(self) -> list[WorkflowRun]:
        # Global paused-run scan (α8.3), oldest first (created_at, id) ASC.
        rows = [r for r in self._runs.values() if r.status == WorkflowRunStatus.PAUSED.value]
        rows.sort(key=lambda r: (r.created_at, str(r.id)))
        return rows

    async def find_paused_by_provider_job_id(self, provider_job_id: str) -> WorkflowRun | None:
        # α8.3b: match the newest checkpoint's ``_paused.provider_job_id`` on a
        # ``paused`` run (mirrors the JSONB path query in the SQL repo).
        checkpoints = sorted(
            self._checkpoints, key=lambda c: (c.created_at, str(c.id)), reverse=True
        )
        for checkpoint in checkpoints:
            state = checkpoint.state if isinstance(checkpoint.state, dict) else {}
            paused = state.get("_paused") if isinstance(state, dict) else None
            if isinstance(paused, dict) and paused.get("provider_job_id") == provider_job_id:
                run = self._runs.get(checkpoint.workflow_run_id)
                if run is not None and run.status == WorkflowRunStatus.PAUSED.value:
                    return run
        return None

    async def cancel(self, project_id: UUID, workflow_run_id: UUID) -> WorkflowRun | None:
        run = self._runs.get(workflow_run_id)
        if run is None or run.project_id != project_id:
            return None
        if not WorkflowRunStatus(run.status).is_cancelable:
            return None
        updated = replace(
            run,
            status=WorkflowRunStatus.CANCELED.value,
            finished_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._runs[workflow_run_id] = updated
        return updated

    # ---- step transitions (status-guarded CAS) ----

    def _cas_step(
        self, workflow_run_id: UUID, step_index: int, allowed: set[str], **changes: Any
    ) -> WorkflowStep | None:
        step = self._find_step(workflow_run_id, step_index)
        if step is None or step.status not in allowed:
            return None
        updated = replace(step, updated_at=datetime.now(UTC), **changes)
        self._steps[step.id] = updated
        return updated

    async def mark_step_running(
        self, workflow_run_id: UUID, step_index: int
    ) -> WorkflowStep | None:
        return self._cas_step(
            workflow_run_id,
            step_index,
            {WorkflowStepStatus.PENDING.value, WorkflowStepStatus.RETRYING.value},
            status=WorkflowStepStatus.RUNNING.value,
            started_at=datetime.now(UTC),
        )

    async def mark_step_succeeded(
        self, workflow_run_id: UUID, step_index: int, output: dict[str, Any]
    ) -> WorkflowStep | None:
        return self._cas_step(
            workflow_run_id,
            step_index,
            {WorkflowStepStatus.RUNNING.value},
            status=WorkflowStepStatus.SUCCEEDED.value,
            output=dict(output),
            finished_at=datetime.now(UTC),
        )

    async def mark_step_retrying(
        self, workflow_run_id: UUID, step_index: int, error: dict[str, Any]
    ) -> WorkflowStep | None:
        step = self._find_step(workflow_run_id, step_index)
        if step is None or step.status != WorkflowStepStatus.RUNNING.value:
            return None
        updated = replace(
            step,
            status=WorkflowStepStatus.RETRYING.value,
            retries=step.retries + 1,
            error=dict(error),
            updated_at=datetime.now(UTC),
        )
        self._steps[step.id] = updated
        return updated

    async def mark_step_failed(
        self, workflow_run_id: UUID, step_index: int, error: dict[str, Any]
    ) -> WorkflowStep | None:
        return self._cas_step(
            workflow_run_id,
            step_index,
            {WorkflowStepStatus.RUNNING.value},
            status=WorkflowStepStatus.FAILED.value,
            error=dict(error),
            finished_at=datetime.now(UTC),
        )

    # ---- checkpoints (append-only) ----

    async def append_checkpoint(
        self, workflow_run_id: UUID, step_index: int, state: dict[str, Any]
    ) -> WorkflowCheckpoint:
        self._ckpt_seq += 1
        checkpoint = WorkflowCheckpoint(
            id=uuid4(),
            workflow_run_id=workflow_run_id,
            step_index=step_index,
            state=dict(state),
            created_at=datetime.now(UTC),
        )
        self._checkpoints.append(checkpoint)
        return checkpoint


# ---- Distributed lock manager (α7.3) ----------------------------------


class FakeDistributedLockManager(IDistributedLockManager):
    """In-memory ``IDistributedLockManager`` modelling the ADR-0041 D8 lease logic.

    Mirrors the observable contract of the SQL manager without a database:
    steal-after-expiry on :meth:`acquire`, owner+live fencing on :meth:`renew`,
    owner fencing on :meth:`release`, and expiry cleanup on :meth:`reclaim_expired`.
    Uses wall-clock ``datetime.now(UTC)`` as its "``now()``". Useful for logic-level
    unit tests; the race/CHECK guarantees are covered by the integration suite.
    """

    def __init__(self) -> None:
        self._locks: dict[str, Lease] = {}

    def _now(self) -> datetime:
        return datetime.now(UTC)

    async def acquire(self, *, key: str, owner: str, lease: timedelta) -> Lease | None:
        if lease.total_seconds() <= 0:
            raise ValueError("lease must be strictly positive")
        now = self._now()
        current = self._locks.get(key)
        if current is not None and current.lease_until > now:
            return None  # held by a live lease — never steal
        acquired = Lease(
            lock_key=key,
            owner=owner,
            lease_until=now + lease,
            heartbeat_at=now,
            acquired_at=now,
        )
        self._locks[key] = acquired
        return acquired

    async def renew(self, lease: Lease, *, lease_for: timedelta) -> Lease | None:
        if lease_for.total_seconds() <= 0:
            raise ValueError("lease must be strictly positive")
        now = self._now()
        current = self._locks.get(lease.lock_key)
        if current is None or current.owner != lease.owner or current.lease_until <= now:
            return None  # lost: released, stolen, or expired past the window
        renewed = replace(current, lease_until=now + lease_for, heartbeat_at=now)
        self._locks[lease.lock_key] = renewed
        return renewed

    async def release(self, lease: Lease) -> bool:
        current = self._locks.get(lease.lock_key)
        if current is not None and current.owner == lease.owner:
            del self._locks[lease.lock_key]
            return True
        return False

    async def reclaim_expired(self, *, now: datetime | None = None) -> int:
        cutoff = now if now is not None else self._now()
        expired = [k for k, v in self._locks.items() if v.lease_until < cutoff]
        for k in expired:
            del self._locks[k]
        return len(expired)


# ---- Usage + pricing repositories (α7.5) ------------------------------


@dataclass
class FakeUsageRecordRepository(IUsageRecordRepository):
    """In-memory ``IUsageRecordRepository`` for α7.5 use-case unit tests.

    Models the observable contract of the real repo: append-only insert that
    raises :class:`DuplicateRequestIdError` on a non-NULL ``request_id`` collision
    (ADR-0033), while NULL ``request_id`` rows always insert and coexist. Stores
    the inserted :class:`NewUsageRecord`s (as :attr:`inserted`) so tests can assert
    the recorder only writes here (W7.5.1) and with the expected priced values.
    """

    inserted: list[NewUsageRecord] = field(default_factory=list)
    _rows: dict[UUID, UsageRecordRow] = field(default_factory=dict)
    _by_request_id: dict[str, UUID] = field(default_factory=dict)

    async def insert(self, new: NewUsageRecord) -> UsageRecordRow:
        if new.request_id is not None and new.request_id in self._by_request_id:
            raise DuplicateRequestIdError(new.request_id)
        row = UsageRecordRow(
            id=uuid4(),
            occurred_at=new.occurred_at,
            tenant_id=new.tenant_id,
            model_id=new.model_id,
            request_id=new.request_id,
            unit=new.unit,
            unit_count=new.unit_count,
            estimated_cost=new.estimated_cost,
            currency=new.currency,
            status=new.status,
            pricing_id=new.pricing_id,
            credits_consumed=new.credits_consumed,
            tokens_prompt=new.tokens_prompt,
            tokens_completion=new.tokens_completion,
            images_count=new.images_count,
            seconds_generated=new.seconds_generated,
        )
        self.inserted.append(new)
        self._rows[row.id] = row
        if new.request_id is not None:
            self._by_request_id[new.request_id] = row.id
        return row

    async def get_by_request_id(self, request_id: str) -> UsageRecordRow | None:
        row_id = self._by_request_id.get(request_id)
        return self._rows.get(row_id) if row_id is not None else None


@dataclass
class FakeModelPricingRepository(IModelPricingRepository):
    """In-memory ``IModelPricingRepository`` for α7.5 use-case unit tests.

    Seed with :meth:`set_price`; :meth:`get_effective` returns the priced unit or
    ``None`` (unconfigured → the recorder prices at 0 and warns, Q5). The
    time-window is not modelled — unit tests exercise the pricing math + the
    missing-pricing path, not the effective-at-time SQL (that is integration).
    """

    _prices: dict[tuple[UUID, str], EffectivePrice] = field(default_factory=dict)

    def set_price(
        self, *, model_id: UUID, unit: str, price_per_unit: str, currency: str = "USD"
    ) -> EffectivePrice:
        ep = EffectivePrice(
            pricing_id=uuid4(),
            unit=unit,
            price_per_unit=Decimal(price_per_unit),
            currency=currency,
        )
        self._prices[(model_id, unit)] = ep
        return ep

    async def get_effective(
        self, *, model_id: UUID, unit: str, at: datetime
    ) -> EffectivePrice | None:
        return self._prices.get((model_id, unit))


# ---- UoW --------------------------------------------------------------


class FakeProviderSettingsRepository(IProviderSettingsRepository):
    """In-memory ``IProviderSettingsRepository`` with tenant-shadows-global reads.

    Seed with :meth:`set_value`; ``get_value`` prefers the tenant-scoped row and
    falls back to the global (``tenant_id is None``) row, mirroring the SQL impl.
    """

    def __init__(self) -> None:
        # keyed by (provider, key, tenant_id)
        self._rows: dict[tuple[str, str, UUID | None], Mapping[str, Any]] = {}

    def set_value(
        self,
        provider: str,
        key: str,
        value: Mapping[str, Any],
        tenant_id: UUID | None = None,
    ) -> None:
        self._rows[(provider, key, tenant_id)] = value

    async def get_value(
        self, provider: str, key: str, tenant_id: UUID | None = None
    ) -> Mapping[str, Any] | None:
        if tenant_id is not None and (provider, key, tenant_id) in self._rows:
            return self._rows[(provider, key, tenant_id)]
        return self._rows.get((provider, key, None))


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
        prompts: FakePromptRepository | None = None,
        media: FakeMediaRepository | None = None,
        timeline: FakeTimelineRepository | None = None,
        render_jobs: FakeRenderJobRepository | None = None,
        export_jobs: FakeExportJobRepository | None = None,
        notifications: FakeNotificationRepository | None = None,
        outbox: FakeEventOutboxRepository | None = None,
        workflow_runs: FakeWorkflowRunRepository | None = None,
        locks: FakeDistributedLockManager | None = None,
        provider_settings: FakeProviderSettingsRepository | None = None,
        usage: FakeUsageRecordRepository | None = None,
        model_pricing: FakeModelPricingRepository | None = None,
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
        self._fake_prompts = prompts or FakePromptRepository()
        self._fake_media = media or FakeMediaRepository()
        self._fake_timeline = timeline or FakeTimelineRepository()
        self._fake_render_jobs = render_jobs or FakeRenderJobRepository()
        # Export jobs derive ownership through render jobs → share the same render fake so a
        # created export resolves to its render's project (mirrors the real join).
        self._fake_export_jobs = export_jobs or FakeExportJobRepository(
            _render_jobs=self._fake_render_jobs
        )
        self._fake_notifications = notifications or FakeNotificationRepository()
        self._fake_outbox = outbox or FakeEventOutboxRepository()
        self._fake_workflow_runs = workflow_runs or FakeWorkflowRunRepository()
        self._fake_locks = locks or FakeDistributedLockManager()
        self._fake_provider_settings = provider_settings or FakeProviderSettingsRepository()
        self._fake_usage = usage or FakeUsageRecordRepository()
        self._fake_model_pricing = model_pricing or FakeModelPricingRepository()
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
        self.prompts = self._fake_prompts
        self.media = self._fake_media
        self.timeline = self._fake_timeline
        self.render_jobs = self._fake_render_jobs
        self.export_jobs = self._fake_export_jobs
        self.notifications = self._fake_notifications
        self.outbox = self._fake_outbox
        self.workflow_runs = self._fake_workflow_runs
        self.locks = self._fake_locks
        self.provider_settings = self._fake_provider_settings
        self.usage = self._fake_usage
        self.model_pricing = self._fake_model_pricing
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
