"""Ports: repository ABCs.

Slice α1 shipped ``IUserRepository`` with two read-only smoke methods
(``count`` / ``exists_by_id``). Slice α2a extends the interface with
entity-returning queries (``get_by_email`` / ``get_by_id``), mutation
methods (``add`` / ``update_last_login``), and introduces the three
sibling ports needed for register + login:

* ``ITenantRepository`` — schema §1
* ``ISessionRepository`` — schema §4 (α2a needs ``add`` only; α2b
  extends with ``get_by_hash`` / ``revoke`` / ``list_family``; α3
  extends with ``get_by_id``)
* ``IRoleRepository``   — schema §5 (α2a needs ``assign_role_by_code``)

Slice α4 extends ``IUserRepository`` with ``update_profile`` — the
version-fenced targeted mutation that underpins ``PATCH /users/me``
and (per α4 pre-flight §10 exit criteria) becomes the canonical
example of an optimistic-concurrency repository CAS.

Per the approved review: repositories answer persistence questions
only. Orchestration (token rotation, replay detection, family
revocation, same-value no-op detection) lives in the use cases, not
in these interfaces.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from app.application.interfaces.publisher import OutboxEvent
from app.application.interfaces.usage_recorder import (
    EffectivePrice,
    NewUsageRecord,
    UsageRecordRow,
)
from app.domain.export.export_job import ExportJob, ExportJobClaim
from app.domain.identity.session import Session
from app.domain.identity.tenant import Tenant
from app.domain.identity.user import User
from app.domain.media.media_asset import MediaAsset
from app.domain.projects.project import Project
from app.domain.prompts.prompt import Prompt
from app.domain.render.render_job import RenderJob
from app.domain.scenes.scene import Scene
from app.domain.timeline.clip import Clip
from app.domain.timeline.timeline import Timeline
from app.domain.timeline.track import Track
from app.domain.versions.project_version import ProjectVersion, ProjectVersionSummary
from app.domain.workflow.workflow_run import WorkflowCheckpoint, WorkflowRun, WorkflowStep


class IUserRepository(ABC):
    """Persistence surface for ``users``. Soft-deleted rows are excluded."""

    # ---- α1 keeper methods (used by /readyz smoke) ----------------------

    @abstractmethod
    async def count(self) -> int:
        """Return the number of non-soft-deleted users."""
        ...

    @abstractmethod
    async def exists_by_id(self, user_id: UUID) -> bool:
        """True iff a non-soft-deleted user row exists with this id."""
        ...

    # ---- α2a additions --------------------------------------------------

    @abstractmethod
    async def get_by_email(self, email: str) -> User | None:
        """Return the first (earliest created) non-soft-deleted user matching ``email``.

        v1 policy note: ``users.email`` is unique **per tenant**, not
        globally, so an email could in principle exist in multiple
        tenants. Because α2a auto-creates a fresh tenant per signup,
        this is unreachable in practice for v1 self-service users. When
        invitation flows (later slice) introduce shared-email users
        across tenants, this repository method stays unchanged — the
        disambiguation moves into the invitation login use case.
        """
        ...

    @abstractmethod
    async def get_by_id(self, user_id: UUID) -> User | None:
        """Return the user with this id, or ``None`` if not found / soft-deleted."""
        ...

    @abstractmethod
    async def add(self, user: User) -> User:
        """Insert a new user row and return the persisted entity.

        Raises ``ConflictError`` if the ``(tenant_id, email)`` uniqueness
        constraint is violated. Timestamps + version are populated by
        DB defaults; the returned entity has the DB-side values.
        """
        ...

    @abstractmethod
    async def update_last_login(self, user_id: UUID, at: datetime) -> None:
        """Set ``users.last_login_at = :at`` for the given user."""
        ...

    # ---- α4 additions ---------------------------------------------------

    @abstractmethod
    async def update_profile(
        self,
        user_id: UUID,
        expected_version: int,
        display_name: str,
    ) -> User | None:
        """Version-fenced profile mutation for ``PATCH /users/me``.

        Performs a compare-and-swap on ``users.version``. In α4 the only
        patchable field is ``display_name``; when later slices add more
        patchable fields (see α4 pre-flight §1.3 non-goals for the
        explicit deferral list), they extend this signature with
        keyword-only optional args rather than introducing a parallel
        method.

        Args:
            user_id: The user whose profile is being updated. The
                caller (``UpdateUserProfile`` use case) has already
                established that this ``user_id`` corresponds to the
                authenticated caller — this repository does NOT
                enforce authorization.
            expected_version: The ``version`` value the caller last
                observed on the ``User`` entity. Used as the CAS fence.
            display_name: The new ``display_name`` value. The caller
                is responsible for whitespace stripping and length
                validation (Pydantic ``UpdateUserProfileRequest`` in
                the API layer). This method treats it as opaque.

        Returns:
            * The updated :class:`~app.domain.identity.user.User` entity
              (with ``version`` incremented, ``updated_at`` bumped) on
              a successful real change.
            * The **unchanged** :class:`User` entity (``version``
              preserved, ``updated_at`` unchanged) on the same-value
              no-op path — when ``display_name`` already equals the
              current row's value. Per α4 pre-flight §D6a
              version-increment invariant: no write, no version bump,
              no dirty replication log. The use case surfaces this
              distinction in structured logs but the wire response
              looks identical to a real change.
            * ``None`` when EITHER the version fence fails OR no live
              (non-soft-deleted) row exists with ``user_id``. Per α4
              §A10, these two outcomes are deliberately
              indistinguishable at the repository boundary — the use
              case raises :class:`ConflictError` for both, the API
              surfaces both as ``412 VERSION_CONFLICT``, and the
              anti-enumeration posture from α3 is preserved.

        This method is the canonical example of the α4 version-fenced
        repository CAS pattern (pre-flight §10 exit criterion 4). All
        future targeted mutations on versioned aggregates follow this
        shape: ``(id, expected_version, ...changes) -> Entity | None``.
        """
        ...


class ITenantRepository(ABC):
    """Persistence surface for ``tenants``. Soft-deleted rows are excluded."""

    @abstractmethod
    async def add(self, tenant: Tenant) -> Tenant:
        """Insert a new tenant row and return the persisted entity.

        Raises ``ConflictError`` if the ``uq_tenants_slug`` uniqueness
        constraint is violated.
        """
        ...

    @abstractmethod
    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:
        """Return the tenant with this id, or ``None`` if not found / soft-deleted."""
        ...

    @abstractmethod
    async def exists_by_slug(self, slug: str) -> bool:
        """True iff a non-soft-deleted tenant exists with ``slug``.

        Used by the ``RegisterUser`` slug-collision retry loop as a
        cheap pre-check; the DB unique constraint remains the
        authoritative gate.
        """
        ...


class ISessionRepository(ABC):
    """Persistence surface for ``sessions``.

    α2a shipped ``add`` only. α2b extends the port with:

    * ``get_by_hash`` — lookup by ``sha256(refresh_jwt)`` for refresh
      flow. Returns the row **regardless** of ``revoked_at``: the caller
      distinguishes "no such token" (None) from "token was revoked"
      (row present with ``revoked_at != NULL``). The latter is the
      reuse-detection signal.
    * ``revoke`` — set ``revoked_at`` on a single row using an atomic
      compare-and-swap (only revokes if ``revoked_at IS NULL``). Returns
      True on the first revoke, False for the already-revoked / missing
      case. Enables logout idempotency + preserves the original logout
      timestamp when the client double-calls.
    * ``list_family`` — enumerate every row sharing a ``family_id``.
      Used by reuse-detection to revoke a whole rotation family after
      one token in it is replayed.

    Rotation orchestration lives in ``RefreshSession``; the port stays
    a persistence surface.
    """

    @abstractmethod
    async def add(self, session: Session) -> Session:
        """Insert a new sessions row and return the persisted entity."""
        ...

    @abstractmethod
    async def get_by_hash(self, token_hash: str) -> Session | None:
        """Return the session whose ``token_hash`` matches (revoked or not), or ``None``.

        Callers MUST inspect ``revoked_at`` to decide whether the token
        is currently valid or is a reuse-signal.
        """
        ...

    @abstractmethod
    async def get_by_id(self, session_id: UUID) -> Session | None:
        """Return the session row for ``session_id`` (revoked or not), or ``None``.

        Introduced by Slice α3 (``get_current_user``). Access tokens
        carry ``sid`` directly in the JWT, so the request-authentication
        path has no token hash to look up by; the sid claim is the
        natural key. Returns revoked rows too, mirroring
        :meth:`get_by_hash` — the caller decides how to interpret
        ``revoked_at`` and ``expires_at`` (α3 dep distinguishes
        ``session_revoked`` vs ``session_expired`` in its structured
        log; both surface as the same generic 401 client-side).
        """
        ...

    @abstractmethod
    async def revoke(self, session_id: UUID, at: datetime) -> bool:
        """Compare-and-swap revoke. Returns True iff this call did the revoke.

        SQL shape: ``UPDATE sessions SET revoked_at = :at
        WHERE id = :sid AND revoked_at IS NULL``. Returning False means
        the row was already revoked (double-logout, race loser, or the
        row does not exist) — safe no-op for the caller.
        """
        ...

    @abstractmethod
    async def list_family(self, family_id: UUID) -> list[Session]:
        """Return every session row sharing ``family_id`` (revoked or not).

        Order is unspecified. Used exclusively by reuse-detection; hot
        paths (refresh happy path) never hit this method.
        """
        ...


class IProjectRepository(ABC):
    """Persistence surface for ``projects``. Soft-deleted rows are excluded.

    Introduced by Slice α5a (create + read). Every method is
    tenant-and-owner scoped: reads pass BOTH ``tenant_id`` AND
    ``owner_user_id`` so a project belonging to another owner (or
    another tenant) is invisible — it returns ``None`` / omits the row
    rather than raising an authorization error, so a caller cannot
    distinguish "does not exist" from "not yours" (α5a D5 + the
    anti-enumeration posture inherited from α3). Authorization is the
    caller's responsibility: the use case has already resolved the
    authenticated ``owner_user_id`` / ``tenant_id`` from
    ``CurrentUserDep`` before reaching this port.

    α5b+ extends this with ``update`` (version-fenced CAS, mirroring
    :meth:`IUserRepository.update_profile`) and ``soft_delete``.
    """

    @abstractmethod
    async def add(self, project: Project) -> Project:
        """Insert a new project row and return the persisted entity.

        Raises ``ConflictError`` if the partial-unique index
        ``uq_projects_tenant_id_owner_user_id_name`` (live rows only) is
        violated — i.e. the owner already has a non-deleted project with
        this ``name``. Timestamps + ``version`` (=1) are populated by DB
        defaults; the returned entity carries the DB-side values.
        """
        ...

    @abstractmethod
    async def get_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> Project | None:
        """Return the owner's live project with ``project_id``, or ``None``.

        ``None`` is returned when the row does not exist, is
        soft-deleted, OR belongs to a different ``tenant_id`` /
        ``owner_user_id`` — the three cases are deliberately
        indistinguishable so ``GET /projects/{id}`` maps all of them to
        a uniform ``404 NOT_FOUND`` (α5a D5).
        """
        ...

    @abstractmethod
    async def get_ownership(self, project_id: UUID) -> tuple[UUID, UUID] | None:
        """Return ``(tenant_id, owner_user_id)`` for a live project by id, or ``None``.

        A **system-only** lookup for server-side event consumers (α8.4a
        generated-media ingestion resolves the owning ``(tenant, user)`` for a
        succeeded run's project so it can register the produced ``MediaAsset`` under
        the correct owner). This is an **implementation detail, not a user-facing
        read** — it is never wired to an HTTP endpoint, so it deliberately sidesteps
        the owner-scoped anti-enumeration posture of :meth:`get_owned`. Returns
        ``None`` when the project is missing or soft-deleted.
        """
        ...

    @abstractmethod
    async def list_owned(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[Project]:
        """Return up to ``limit`` of the owner's live projects, newest first.

        Ordering is ``created_at DESC, id DESC`` — a *total* order so
        keyset pagination never duplicates or skips a row under
        timestamp ties (α5a D14). ``after`` is the decoded cursor
        position ``(created_at, id)``: when provided, only rows strictly
        *after* it in the DESC order are returned (Postgres row-value
        comparison ``(created_at, id) < (:created_at, :id)``). The
        caller (``ListProjects``) requests ``limit + 1`` to detect
        whether a further page exists, then trims + builds the
        next-page cursor.
        """
        ...

    @abstractmethod
    async def update_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> Project | None:
        """Version-fenced partial update of the owner's live project (α5b).

        Applies ``changes`` (a mapping of ``projects`` business columns —
        ``name`` / ``description`` / ``language`` / ``style`` /
        ``settings``) to the row matching ``project_id`` + ``tenant_id`` +
        ``owner_user_id`` + ``deleted_at IS NULL`` + ``version =
        expected_version``, via a compare-and-swap ``UPDATE ... WHERE
        version = :expected`` (mirrors
        :meth:`IUserRepository.update_profile`).

        Returns the updated :class:`Project` (with the trigger-bumped
        ``version`` / ``updated_at``) on success, or ``None`` when the CAS
        matched no row — i.e. a concurrent writer bumped ``version`` or
        soft-deleted the row between the caller's ``get_owned`` and this
        write. The use case (``UpdateProject``) has ALREADY established
        visibility via ``get_owned`` (the 404-before-412 split, α5b D3), so
        a ``None`` here is a *concurrency* outcome (→ ``412``), never a
        *visibility* one.

        Raises ``ConflictError`` if the update renames the project to a
        ``name`` already held by another live project of the same
        ``(tenant_id, owner_user_id)`` — the partial-unique index
        ``uq_projects_tenant_id_owner_user_id_name`` violation is caught
        and surfaced as ``409`` (α5b D9), identical to ``add``.

        ``changes`` MUST contain only mutable business columns; callers
        never pass ``version`` / ``updated_at`` (trigger-owned) or
        identity/ownership columns. An empty ``changes`` is a caller bug
        (the use case resolves the same-value no-op before reaching here).
        """
        ...

    @abstractmethod
    async def soft_delete_owned(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> bool:
        """Soft-delete (``deleted_at = now()``) the owner's live project (α5b).

        Scoped to ``tenant_id`` + ``owner_user_id`` + ``deleted_at IS
        NULL`` exactly like ``get_owned``. Returns ``True`` if a live owned
        row was found and marked deleted, ``False`` otherwise (row missing,
        already soft-deleted, or owned by another user/tenant). The use
        case (``DeleteProject``) maps ``False`` → ``404 NOT_FOUND`` so a
        repeat delete — and any GET/PATCH after delete — is a uniform
        ``404`` (idempotent-by-404, α5b D6). No version fence (α5b D8).
        """
        ...

    @abstractmethod
    async def touch_version(
        self,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> int | None:
        """Bump the aggregate OCC token (``projects.version += 1``) — α5d.2.

        The **Aggregate OCC Rule** (α5d.2 Q1 / Option A;
        ``PROJECT_AGGREGATE.md`` §6): ``projects.version`` is the
        optimistic-concurrency token for the *entire* Project aggregate, so
        any mutation of an aggregate child that changes externally observable
        project state MUST advance it. The α5c scene use cases
        (create / update / move / delete) call this after a **real** child
        change so a subsequent restore fence (α5d.2) "sees" the edit.

        Owner-and-tenant scoped + ``deleted_at IS NULL`` exactly like
        ``get_owned``. Hand-sets ``version = version + 1`` over the guarded
        ``tg_projects_biu_version_bump`` trigger (net +1, same discipline as
        ``update_owned``). Returns the new ``version`` on success, or ``None``
        if no live owned row matched (the caller established ownership
        upstream, so ``None`` means a concurrent soft-delete — surfaced by the
        transaction, never a silent no-op). MUST NOT be called for a
        same-value / no-op child mutation (the aggregate version only moves on
        a real persisted change).
        """
        ...


class ISceneRepository(ABC):
    """Persistence surface for ``scenes``. Soft-deleted rows are excluded.

    Introduced by Slice α5c. A scene lives under a project's **implicit
    default storyboard** (``Project → Storyboard → Scene``, D1): the
    storyboard is resolved server-side and never appears on the wire. Every
    method is project-scoped — the use case has ALREADY established project
    ownership via :meth:`IProjectRepository.get_owned` (the two-level
    visibility gate, α5c D6) before reaching this port, so a scene that
    belongs to another project simply returns ``None`` / is omitted /
    reports ``False`` (anti-enumeration, inherited from α3/α5a).

    Ordering is a **sparse gap-based** integer key ``scene_number`` (1000,
    2000, … — D3), never exposed raw; :meth:`position_of` projects a dense
    1-based position from the sorted order (Q6). ``ensure_default_storyboard``
    and the numbering/reorder mutations serialize on a ``SELECT … FOR
    UPDATE`` lock of the parent ``projects`` row (D9), because ``storyboards``
    carries no per-project uniqueness and ``scenes`` no per-storyboard
    ordering lock — the row lock is the concurrency boundary that guarantees
    exactly one default storyboard and collision-free ``scene_number``
    assignment.
    """

    @abstractmethod
    async def ensure_default_storyboard(self, project_id: UUID) -> tuple[UUID, bool]:
        """Resolve (get-or-create) the project's single default storyboard.

        Takes a ``SELECT … FOR UPDATE`` lock on the parent ``projects`` row
        (held for the transaction) so concurrent first-scene creations
        cannot each insert a storyboard (D9). Returns
        ``(storyboard_id, created)`` where ``created`` is ``True`` only when
        this call inserted the storyboard (the caller logs
        ``storyboard.default_created`` on ``True``). Idempotent: the earliest
        live storyboard is reused if one already exists.
        """
        ...

    @abstractmethod
    async def add(
        self,
        *,
        storyboard_id: UUID,
        title: str,
        duration_seconds: float,
        narration: str | None,
        subtitle: str | None,
    ) -> Scene:
        """Append a new scene to ``storyboard_id`` and return the persisted entity.

        Unlike :meth:`IProjectRepository.add` (which takes a fully-formed
        entity), this takes explicit content fields because ``scene_number``
        is **server-computed**: the new scene is appended after the current
        maximum live ``scene_number`` (``max + 1000``, or ``1000`` for the
        first scene — D10). The caller MUST hold the project-row lock (via a
        prior :meth:`ensure_default_storyboard` in the same transaction) so
        the ``max`` read + insert is race-free. ``id`` / ``version`` (=1) /
        timestamps are DB-populated; the returned entity carries the
        authoritative values.
        """
        ...

    @abstractmethod
    async def list_by_project(self, project_id: UUID) -> list[Scene]:
        """Return the project's live scenes ordered by ``scene_number`` ASC.

        Read-only and side-effect-free: if the project has no storyboard yet
        (no scene has ever been created), returns ``[]`` **without** creating
        one (D8) — a GET must never mutate. Not paginated (Q2): a project's
        scene set is a bounded editorial list.
        """
        ...

    @abstractmethod
    async def get_owned_scene(self, project_id: UUID, scene_id: UUID) -> Scene | None:
        """Return the project's live scene with ``scene_id``, or ``None``.

        ``None`` when the scene is missing, soft-deleted, or belongs to a
        storyboard of a different project — deliberately indistinguishable so
        ``GET /projects/{id}/scenes/{scene_id}`` maps all of them to a
        uniform ``404`` (α5c D6, mirroring α5a D5). The join to
        ``storyboards`` enforces the cross-project isolation even though
        project ownership was already checked upstream (defence in depth).
        """
        ...

    @abstractmethod
    async def update_owned(
        self,
        project_id: UUID,
        scene_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> Scene | None:
        """Version-fenced partial update of the project's live scene (α5c).

        Applies ``changes`` (a mapping of ``scenes`` content columns —
        ``title`` / ``duration_seconds`` / ``narration`` / ``subtitle``) to
        the row matching ``scene_id`` + project (via storyboard join) +
        ``deleted_at IS NULL`` + ``version = expected_version`` via a
        compare-and-swap (mirrors :meth:`IProjectRepository.update_owned`,
        hand-setting ``version + 1`` over the guarded trigger → net +1).
        Returns the updated :class:`Scene` on success, or ``None`` when the
        CAS matched no row (concurrent bump/delete after the use case's
        ``get_owned_scene`` → ``412``; the visibility 404 was already
        decided upstream, α5c D6). Never changes ``scene_number`` (ordering
        is :meth:`reorder_owned`'s job — D11).
        """
        ...

    @abstractmethod
    async def reorder_owned(
        self,
        project_id: UUID,
        scene_id: UUID,
        target_position: int,
        expected_version: int,
    ) -> Scene | None:
        """Move ``scene_id`` to 1-based ``target_position`` (version-fenced).

        Takes the project-row lock (D9) and recomputes ``scene_number`` from
        the sorted live scenes: the gap midpoint between the target
        neighbours (D12). ``target_position`` is clamped to the valid range.
        A move to the scene's current slot is a **no-op** (returns the
        unchanged entity, no write). When no integer gap remains between
        neighbours, the whole storyboard is **rebalanced** to fresh 1000-step
        numbers (D12) — the moved scene's number is set under the version
        fence; the other scenes' ``version`` are trigger-bumped (D14,
        accepted). Returns the moved :class:`Scene` on success, or ``None``
        when the version fence fails (concurrent content-PATCH bumped the
        moved scene, or a concurrent delete) → ``412``.
        """
        ...

    @abstractmethod
    async def soft_delete_owned(self, project_id: UUID, scene_id: UUID) -> bool:
        """Soft-delete (``deleted_at = now()``) the project's live scene (α5c).

        Scoped to the project (via storyboard join) + ``deleted_at IS NULL``.
        Returns ``True`` if a live scene was found and marked, ``False``
        otherwise (missing, already soft-deleted, or another project's
        scene). The use case maps ``False`` → ``404`` so a repeat delete —
        and any GET/PATCH/move after delete — is a uniform ``404``
        (idempotent-by-404, α5c D13). No version fence (D13). Leaves a gap in
        ``scene_number`` (the neighbours are not renumbered — display
        ``position`` is recomputed dynamically).
        """
        ...

    @abstractmethod
    async def position_of(self, storyboard_id: UUID, scene_number: int) -> int:
        """Return the 1-based display position of ``scene_number`` in its storyboard.

        Computed as ``count(live scenes with a smaller scene_number) + 1``.
        Used to project the dense wire ``position`` for single-scene
        responses (create/get/patch/move) without exposing the raw sparse
        ``scene_number`` (Q6); the list endpoint enumerates positions from
        the already-sorted :meth:`list_by_project` result instead.
        """
        ...


class IPromptRepository(ABC):
    """Persistence surface for ``prompts``. Soft-deleted rows are excluded.

    Introduced by Slice α6.1. A prompt is a **generation input** owned by a
    project (the table carries no ``tenant_id`` / ``owner_user_id``; ownership
    is derived through ``project_id``). Every method is project-scoped — the use
    case has ALREADY established project ownership via
    :meth:`IProjectRepository.get_owned` before reaching this port (the α5c
    two-level visibility gate), so a prompt of another project simply returns
    ``None`` / is omitted / reports ``False`` (anti-enumeration).

    **No optimistic concurrency (ADR-0036 / α6.1 Q1 = Option A).** The
    ``prompts`` table has no ``version`` column and is absent from the
    version-bump trigger set, so prompt mutations are **last-writer-wins**: no
    CAS fence, and — crucially — a prompt create/update/delete does **NOT** bump
    ``projects.version`` and is **NOT** captured in project version snapshots.
    Prompts are generation inputs, not versioned editorial content; the versioned
    aggregate stays {project root + scenes}. ``updated_at`` is trigger-owned.
    """

    @abstractmethod
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
        """Insert a new prompt under ``project_id`` and return the persisted entity.

        ``scene_id`` / ``model_id`` may be ``None`` (a project-level prompt / an
        unpinned prompt). The caller (``CreatePrompt``) has ALREADY validated
        that a non-``None`` ``scene_id`` references a live scene in this project
        (via :meth:`ISceneRepository.get_owned_scene`) and that a non-``None``
        ``model_id`` is linkable (via :meth:`model_is_linkable`). ``id`` /
        timestamps are DB-populated; the returned entity carries the
        authoritative values. ``generated_by_agent`` is left ``NULL``
        (server-owned provenance, α8 — not part of α6.1).
        """
        ...

    @abstractmethod
    async def list_owned(
        self,
        project_id: UUID,
        *,
        kind: str | None = None,
        scene_id: UUID | None = None,
    ) -> list[Prompt]:
        """Return the project's live prompts, newest first, optionally filtered.

        Ordered by ``created_at DESC, id DESC`` (a total order — no duplicate /
        skip under timestamp ties). ``kind`` (a ``prompt_kind`` value) and
        ``scene_id`` narrow the result when provided (combined = AND). Not
        paginated in α6.1 (Q9 — a project's prompt set is a bounded working
        list). Side-effect-free.
        """
        ...

    @abstractmethod
    async def get_owned(self, project_id: UUID, prompt_id: UUID) -> Prompt | None:
        """Return the project's live prompt with ``prompt_id``, or ``None``.

        ``None`` when the prompt is missing, soft-deleted, or belongs to a
        different project — deliberately indistinguishable so
        ``GET /projects/{id}/prompts/{prompt_id}`` maps all of them to a uniform
        ``404`` (α6.1 D2, mirroring α5c D6).
        """
        ...

    @abstractmethod
    async def update_owned(
        self,
        project_id: UUID,
        prompt_id: UUID,
        changes: Mapping[str, Any],
    ) -> Prompt | None:
        """Partial update of the project's live prompt (α6.1).

        Applies ``changes`` (a mapping of mutable columns — ``text_content`` /
        ``kind`` / ``model_id`` / ``extra``) to the row matching ``prompt_id`` +
        ``project_id`` + ``deleted_at IS NULL``. **No version fence** (ADR-0036 /
        Q1 = Option A — prompts have no OCC column); ``updated_at`` is bumped by
        the trigger. Returns the updated :class:`Prompt`, or ``None`` when no
        live owned row matched (missing / soft-deleted / another project's — the
        use case maps ``None`` → ``404``, never ``412``: there is no concurrency
        outcome to surface). ``changes`` MUST be non-empty and contain only
        mutable columns (the use case resolves the empty-patch ``422`` upstream);
        a non-``None`` ``model_id`` in ``changes`` was validated linkable by the
        use case first.
        """
        ...

    @abstractmethod
    async def soft_delete_owned(self, project_id: UUID, prompt_id: UUID) -> bool:
        """Soft-delete (``deleted_at = now()``) the project's live prompt (α6.1).

        Scoped to ``project_id`` + ``deleted_at IS NULL``. Returns ``True`` if a
        live prompt was found and marked, ``False`` otherwise (missing, already
        soft-deleted, or another project's prompt). The use case maps ``False``
        → ``404`` so a repeat delete — and any GET/PATCH after delete — is a
        uniform ``404`` (idempotent-by-404, α6.1 D4). No version fence.
        """
        ...

    @abstractmethod
    async def model_is_linkable(self, model_id: UUID) -> bool:
        """True iff ``model_id`` references an ``ai_models`` row usable as a prompt target.

        A model is linkable when the row exists and its ``status`` is **not**
        ``retired`` (α6.1 Q4). Used by ``CreatePrompt`` / ``UpdatePrompt`` to
        validate a client-supplied ``model_id`` → ``422`` on failure, before the
        row is written (``prompts.model_id`` is ``ON DELETE SET NULL`` so the FK
        alone would silently accept a since-retired model; this is the app-level
        gate). ``ai_models`` is a system registry with no soft-delete.
        """
        ...


class IMediaRepository(ABC):
    """Persistence surface for ``media_assets``. Soft-deleted rows are excluded.

    Introduced by Slice α6.2. A media asset is a **generation output** —
    a registered pointer to a concrete stored object. Unlike prompts/scenes, the
    ``media_assets`` row carries its **own** ``tenant_id`` + ``owner_user_id``
    (direct ownership), so every method is **owner-scoped** (``tenant_id`` +
    ``owner_user_id``), NOT project-scoped: an asset owned by another user (or in
    another tenant) is invisible — it returns ``None`` / is omitted / reports
    ``False`` (anti-enumeration, inherited from α5a). ``project_id`` is an
    optional link/filter, never the access key. Authorization is the caller's
    responsibility: the use case has already resolved ``owner_user_id`` /
    ``tenant_id`` from ``CurrentUserDep`` before reaching this port.

    **No optimistic concurrency (ADR-0037, adopting ADR-0036 / α6.2 Q3).** The
    ``media_assets`` table has no ``version`` column and is absent from the
    version-bump trigger set, so media mutations are **last-writer-wins**: no CAS
    fence, and — crucially — a media register/update/delete does **NOT** bump
    ``projects.version`` and is **NOT** captured in project version snapshots.
    Media is a generation output, not versioned editorial content; the versioned
    aggregate stays {project root + scenes}. ``updated_at`` is trigger-owned.
    """

    @abstractmethod
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
        """Insert a new media asset for ``(tenant_id, owner_user_id)`` and return it.

        The caller (``RegisterMedia``) has ALREADY validated each non-``None``
        link (``project_id`` owned live, ``scene_id`` / ``prompt_id`` live in that
        project, ``model_id`` linkable) and that ``source`` is one of the
        register-allowed values (α6.2 Q2 — ``generated`` is rejected upstream).
        ``id`` / timestamps are DB-populated; the returned entity carries the
        authoritative values.

        Raises ``ConflictError`` if the ``uq_media_assets_storage_backend_
        storage_bucket_storage_key`` uniqueness constraint is violated — i.e. an
        asset with the same ``(storage_backend, storage_bucket, storage_key)``
        already exists (the use case maps it to ``409``, α6.2 Q6). The unique
        constraint is the race-safe backstop behind the use case's pre-check.
        """
        ...

    @abstractmethod
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
        """Return the owner's live media assets, newest first, optionally filtered.

        Ordered by ``created_at DESC, id DESC`` (a total order — no duplicate /
        skip under timestamp ties). ``kind`` (a ``media_kind`` value), ``source``
        (a ``media_source`` value), ``project_id`` and ``scene_id`` narrow the
        result when provided (combined = AND). Uses the ``(tenant_id, kind,
        created_at)`` index. Not paginated in α6.2 (Q10). Side-effect-free.
        """
        ...

    @abstractmethod
    async def get_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> MediaAsset | None:
        """Return the owner's live media asset with ``media_id``, or ``None``.

        ``None`` when the row is missing, soft-deleted, OR belongs to a different
        ``tenant_id`` / ``owner_user_id`` — deliberately indistinguishable so
        ``GET /media/{media_id}`` maps all of them to a uniform ``404`` (α6.2 D2,
        mirroring α5a D5).
        """
        ...

    @abstractmethod
    async def list_enrichable_generated_videos(
        self, *, target_version: int, limit: int
    ) -> list[MediaAsset]:
        """Return live **primary** generated video assets below ``target_version`` (α8.4c/d).

        The media-enrichment poll ingress (mirrors ``list_paused`` / ``list_claimable``):
        ``kind='video' AND source='generated' AND deleted_at IS NULL`` **AND** the asset
        is *primary* (recursion guard, W8.4d.1 — ``NOT (source_metadata ?
        'parent_media_asset_id')``, so derived assets are never enrichment inputs)
        **AND** its enrichment version is stale
        (``COALESCE((source_metadata #>> '{enrichment,version}')::int, 0) <
        target_version``), ordered ``created_at ASC, id ASC``, capped at ``limit``.

        Owner-agnostic (server-side worker). Version-based (α8.4d Fork D): bumping
        ``CURRENT_ENRICHMENT_VERSION`` re-claims already-enriched assets so new derived
        artifacts backfill; α8.4c markers (no ``version``) count as ``0``. An asset
        **drops out** once its marker reaches ``target_version`` — the set is bounded
        and shrinking. Side-effect-free.
        """
        ...

    @abstractmethod
    async def get_by_storage_coords(
        self,
        *,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
    ) -> MediaAsset | None:
        """Return the live media asset at these physical storage coordinates, or ``None``.

        Additive read introduced by α8.4b (α8.4a for generated ingestion could get
        away without it; the render worker needs it). The physical-object columns
        (``storage_backend`` / ``storage_bucket`` / ``storage_key``) are immutable
        and, for deterministic-key producers (ingestion, render), unique per
        artifact — so this is the idempotent-recovery lookup: after ``add`` raises
        ``ConflictError`` (the artifact was already registered on a prior attempt),
        the worker re-reads the existing asset by its deterministic coords to obtain
        its ``id``. Owner-agnostic on purpose (server-side worker context, like
        ``get_ownership`` on projects). Side-effect-free; soft-deleted rows excluded.
        """
        ...

    @abstractmethod
    async def update_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        changes: Mapping[str, Any],
    ) -> MediaAsset | None:
        """Partial update of the owner's live media asset (α6.2, narrow PATCH).

        Applies ``changes`` (a mapping of the **mutable** columns only —
        ``project_id`` / ``scene_id`` / ``prompt_id`` / ``model_id`` /
        ``provider`` / ``source_metadata``; the physical-object columns are
        immutable, Q8) to the row matching ``media_id`` + ``tenant_id`` +
        ``owner_user_id`` + ``deleted_at IS NULL``. **No version fence**
        (ADR-0037 — media has no OCC column); ``updated_at`` is bumped by the
        trigger. Returns the updated :class:`MediaAsset`, or ``None`` when no live
        owned row matched (missing / soft-deleted / another owner's — the use
        case maps ``None`` → ``404``, never ``412``). ``changes`` MUST be
        non-empty and contain only mutable columns (the use case resolves the
        empty-patch ``422`` upstream); any non-``None`` link in ``changes`` was
        validated by the use case first.
        """
        ...

    @abstractmethod
    async def soft_delete_owned(
        self,
        media_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> bool:
        """Soft-delete (``deleted_at = now()``) the owner's live media asset (α6.2).

        Scoped to ``tenant_id`` + ``owner_user_id`` + ``deleted_at IS NULL``.
        Returns ``True`` if a live owned row was found and marked, ``False``
        otherwise (missing, already soft-deleted, or another owner's asset). The
        use case maps ``False`` → ``404`` so a repeat delete — and any GET/PATCH
        after delete — is a uniform ``404`` (idempotent-by-404, α6.2 D4). No
        version fence. Soft-delete trips none of the downstream ``SET NULL`` FKs
        (they fire on hard delete only), so any future clip/render references
        keep pointing at the now-hidden row (F6, forward-compat note).
        """
        ...

    @abstractmethod
    async def model_is_linkable(self, model_id: UUID) -> bool:
        """True iff ``model_id`` references an ``ai_models`` row usable as a media target.

        A model is linkable when the row exists and its ``status`` is **not**
        ``retired`` (α6.2 Q5, mirroring α6.1 Q4). Used by ``RegisterMedia`` /
        ``UpdateMedia`` to validate a client-supplied ``model_id`` → ``422`` on
        failure, before the row is written (``media_assets.model_id`` is ``ON
        DELETE RESTRICT``, but the FK alone would still accept a since-retired
        model; this is the app-level gate). ``ai_models`` has no soft-delete.
        """
        ...


class ITimelineRepository(ABC):
    """Persistence surface for ``timelines`` + ``tracks``. Introduced by Slice α6.3a.

    The **Timeline aggregate** is a *self-contained OCC aggregate* (ADR-0038): the
    ``Timeline`` is the root (1:1 with a project — the ``timelines`` partial-unique
    index on ``project_id`` enforces one live timeline per project), and ``Track``
    (and — α6.3b — ``Clip``) are its children. Ownership is **derived through the
    project** (the tables carry no ``tenant_id`` / ``owner_user_id``); the use case
    has ALREADY established project ownership via
    :meth:`IProjectRepository.get_owned` before reaching this port, so a timeline /
    track of another project simply returns ``None`` / is omitted / reports
    ``False`` (anti-enumeration, inherited from α5a/α5c).

    **Single-token optimistic concurrency (ADR-0038 / Q1 / Q13).** Only the
    ``timelines`` row carries a ``version`` (``VersionMixin`` + the guarded
    ``tg_timelines_biu_version_bump`` trigger); ``tracks`` do **not**. Therefore
    ``timelines.version`` is the OCC token for the *entire* aggregate:

    * :meth:`update_owned` is the version-fenced CAS on the root's own columns
      (mirrors :meth:`IProjectRepository.update_owned`).
    * :meth:`bump_version` is the aggregate roll-up a child (track/clip) mutation
      calls to advance the token — optionally fenced (``expected_version`` given →
      CAS → ``None`` on mismatch → ``412``; ``None`` → unconditional bump, used by
      child ``POST`` where a create cannot be harmfully stale, Q13).

    The aggregate is **excluded** from the project-version ledger (ADR-0035):
    timeline/track mutations do **NOT** bump ``projects.version`` and are **NOT**
    captured in ``project_versions`` snapshots. ``updated_at`` is trigger-owned.
    """

    # ---- timeline root -------------------------------------------------

    @abstractmethod
    async def add(
        self,
        *,
        project_id: UUID,
        aspect_ratio: str,
        frame_rate: int,
        background_color: str,
    ) -> Timeline:
        """Insert the project's single timeline and return the persisted entity.

        ``version`` (=1) / timestamps are DB-populated; ``project_version_id`` is
        left ``NULL`` (its write path is deferred to α7+, ADR-0035). Raises
        ``ConflictError`` if the partial-unique index ``uq_timelines_project_id``
        (live rows only) is violated — i.e. the project already has a live
        timeline (the use case maps it to ``409``, Q3). The unique index is the
        race-safe backstop; a project is 1:1 with its timeline.
        """
        ...

    @abstractmethod
    async def get_by_project(self, project_id: UUID) -> Timeline | None:
        """Return the project's live timeline, or ``None`` if none exists yet.

        ``None`` when the project has no timeline (not yet provisioned) or it is
        soft-deleted. The caller has already established project ownership, so a
        ``None`` here is "no timeline" → the use case maps it to ``404`` (Q3).
        """
        ...

    @abstractmethod
    async def update_owned(
        self,
        project_id: UUID,
        expected_version: int,
        changes: Mapping[str, Any],
    ) -> Timeline | None:
        """Version-fenced partial update of the project's live timeline (α6.3a).

        Applies ``changes`` (root columns — ``aspect_ratio`` / ``frame_rate`` /
        ``background_color`` / ``duration_seconds``) to the row matching
        ``project_id`` + ``deleted_at IS NULL`` + ``version = expected_version``
        via a compare-and-swap (mirrors :meth:`IProjectRepository.update_owned`,
        hand-setting ``version + 1`` over the guarded trigger → net +1). Returns
        the updated :class:`Timeline` on success, or ``None`` when the CAS matched
        no row — a concurrent bump/delete after the use case's ``get_by_project``
        (the visibility ``404`` was decided upstream) → ``412``. ``changes`` MUST
        be non-empty and contain only mutable root columns.
        """
        ...

    @abstractmethod
    async def bump_version(
        self,
        project_id: UUID,
        expected_version: int | None,
    ) -> int | None:
        """Advance the aggregate OCC token after a child (track/clip) mutation.

        The **single-token rule** (ADR-0038 / Q13): a track/clip create/update/
        delete changes the aggregate, so it advances ``timelines.version``. Two
        modes:

        * ``expected_version is None`` — **unconditional** bump (used by child
          ``POST``; a create cannot be harmfully stale). Returns the new
          ``version`` (or ``None`` only if the timeline vanished concurrently).
        * ``expected_version`` given — **fenced** CAS (``WHERE version =
          expected``); returns the new ``version`` on match, or ``None`` on a
          stale token (the use case maps ``None`` → ``412``). Used by child
          ``PATCH`` / ``DELETE``.

        Hand-sets ``version + 1`` over the guarded ``tg_timelines_biu_version_bump``
        trigger (net +1). Scoped ``project_id`` + ``deleted_at IS NULL``. Does
        **NOT** touch ``projects.version`` (ADR-0035).
        """
        ...

    # ---- tracks --------------------------------------------------------

    @abstractmethod
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
        """Insert a track under ``timeline_id`` and return the persisted entity.

        ``id`` / timestamps are DB-populated. Raises ``ConflictError`` if the
        partial-unique index ``uq_tracks_timeline_id_z_index`` (live rows) is
        violated — i.e. another live track of this timeline already holds
        ``z_index`` (the use case maps it to ``409``, Q5). Does **not** bump the
        timeline version — the use case calls :meth:`bump_version` for the
        aggregate roll-up in the same transaction (ADR-0038).
        """
        ...

    @abstractmethod
    async def list_tracks(self, timeline_id: UUID) -> list[Track]:
        """Return the timeline's live tracks ordered by ``z_index`` ASC.

        Read-only and side-effect-free. Soft-deleted tracks are excluded. Not
        paginated (a timeline's track set is a bounded editorial list).
        """
        ...

    @abstractmethod
    async def get_track(self, timeline_id: UUID, track_id: UUID) -> Track | None:
        """Return the timeline's live track with ``track_id``, or ``None``.

        ``None`` when the track is missing, soft-deleted, or belongs to a
        different timeline — deliberately indistinguishable so the route maps all
        of them to a uniform ``404`` (mirroring α5c D6).
        """
        ...

    @abstractmethod
    async def update_track(
        self,
        timeline_id: UUID,
        track_id: UUID,
        changes: Mapping[str, Any],
    ) -> Track | None:
        """Partial update of the timeline's live track (α6.3a).

        Applies ``changes`` (mutable columns — ``kind`` / ``z_index`` / ``name`` /
        ``locked`` / ``muted``) to the row matching ``track_id`` + ``timeline_id``
        + ``deleted_at IS NULL``. Tracks have **no own version** — the OCC fence is
        the parent timeline's, applied by the use case via :meth:`bump_version`
        (ADR-0038). Returns the updated :class:`Track`, or ``None`` when no live
        row matched (concurrent delete → the use case maps it to ``404``). Raises
        ``ConflictError`` if the new ``z_index`` collides with another live track
        of the timeline (``409``, Q5). ``changes`` MUST be non-empty and contain
        only mutable columns.
        """
        ...

    @abstractmethod
    async def soft_delete_track(self, timeline_id: UUID, track_id: UUID) -> bool:
        """Soft-delete (``deleted_at = now()``) the timeline's live track (α6.3a).

        Scoped ``timeline_id`` + ``deleted_at IS NULL``. Returns ``True`` if a live
        track was found and marked, ``False`` otherwise (missing, already
        soft-deleted, or another timeline's track). The use case maps ``False`` →
        ``404`` so a repeat delete — and any GET/PATCH after delete — is a uniform
        ``404`` (idempotent-by-404). Frees the ``z_index`` slot (the partial-unique
        index only covers live rows). The aggregate roll-up
        (:meth:`bump_version`) is the use case's job.
        """
        ...

    # ---- clips (α6.3b) -------------------------------------------------

    @abstractmethod
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
        """Insert a clip under ``track_id`` and return the persisted entity (α6.3b).

        ``id`` / timestamps are DB-populated; ``transition_*`` / ``effects`` take
        their server defaults (``NULL`` / ``[]``) — their write paths are deferred
        to α6.4 (D9). ``media_asset_id`` link validity is the use case's job
        (owned + live → ``422``, D4); this port only persists. Clips may overlap
        and share ``start_seconds`` — there is no unique constraint (Q6). Does
        **not** bump the timeline version — the use case calls
        :meth:`bump_version` for the aggregate roll-up in the same transaction.
        """
        ...

    @abstractmethod
    async def list_clips(self, track_id: UUID) -> list[Clip]:
        """Return the track's live clips ordered by ``start_seconds`` ASC, ``id`` ASC.

        Read-only and side-effect-free. Soft-deleted clips are excluded. The
        ``id`` tiebreak makes the order *total* (deterministic under equal
        ``start_seconds``). Not paginated (a track's clip set is a bounded
        editorial list).
        """
        ...

    @abstractmethod
    async def list_clips_for_timeline(self, timeline_id: UUID) -> dict[UUID, list[Clip]]:
        """Return all live clips of the timeline's tracks, grouped by ``track_id``.

        A single query (join ``clips`` → ``tracks``) so the composition tree
        (``GET …/timeline`` / ``GET …/tracks``) embeds each track's clips without
        an N+1 per-track fan-out. Each list is ordered ``start_seconds`` ASC,
        ``id`` ASC; tracks with no live clips are simply absent from the mapping
        (the caller defaults to ``[]``). Soft-deleted clips/tracks are excluded.
        """
        ...

    @abstractmethod
    async def get_clip(self, track_id: UUID, clip_id: UUID) -> Clip | None:
        """Return the track's live clip with ``clip_id``, or ``None``.

        ``None`` when the clip is missing, soft-deleted, or belongs to a different
        track — deliberately indistinguishable so the route maps all of them to a
        uniform ``404`` (mirroring α5c D6 / α6.3a ``get_track``).
        """
        ...

    @abstractmethod
    async def update_clip(
        self,
        track_id: UUID,
        clip_id: UUID,
        changes: Mapping[str, Any],
    ) -> Clip | None:
        """Partial update of the track's live clip (α6.3b).

        Applies ``changes`` (mutable columns — ``media_asset_id`` /
        ``start_seconds`` / ``end_seconds`` / ``source_start_seconds`` /
        ``source_end_seconds`` / ``volume`` / ``locked``) to the row matching
        ``clip_id`` + ``track_id`` + ``deleted_at IS NULL``. ``track_id`` is
        immutable (no cross-track move, Q4). Clips have **no own version** — the
        OCC fence is the parent timeline's, applied by the use case via
        :meth:`bump_version` (ADR-0038). Returns the updated :class:`Clip`, or
        ``None`` when no live row matched (concurrent delete → ``404``).
        ``changes`` MUST be non-empty and contain only mutable columns; the DB
        CHECKs (``start >= 0``, ``end > start``, ``volume`` 0–4) are the backstop
        for the DTO validation.
        """
        ...

    @abstractmethod
    async def soft_delete_clip(self, track_id: UUID, clip_id: UUID) -> bool:
        """Soft-delete (``deleted_at = now()``) the track's live clip (α6.3b).

        Scoped ``track_id`` + ``deleted_at IS NULL``. Returns ``True`` if a live
        clip was found and marked, ``False`` otherwise (missing, already
        soft-deleted, or another track's clip). The use case maps ``False`` →
        ``404`` so a repeat delete — and any GET/PATCH after delete — is a uniform
        ``404`` (idempotent-by-404). The aggregate roll-up (:meth:`bump_version`)
        is the use case's job.
        """
        ...


class IProjectVersionRepository(ABC):
    """Persistence surface for ``project_versions``. Introduced by Slice α5d.

    Snapshots are **append-only and immutable** (DB ``reject_mutation``
    trigger, α5d DS7): there is no ``update`` / ``delete`` — the only write is
    :meth:`create_snapshot`. Every method is project-scoped; the use case has
    ALREADY established project ownership via
    :meth:`IProjectRepository.get_owned` before reaching this port (the same
    two-level visibility gate as α5c), so a version that belongs to another
    project simply returns ``None`` / is omitted (anti-enumeration, inherited
    from α3/α5a).

    The port owns the snapshot ASSEMBLY (reading the live project + default
    storyboard + ordered scenes and denormalizing them into the canonical
    JSONB blob), the monotonic ``version_number`` assignment, the
    ``parent_version_id`` lineage link, and the ``projects.current_version_id``
    pointer advance — all under a ``SELECT … FOR UPDATE`` lock of the parent
    ``projects`` row (α5d Q6) so concurrent captures cannot collide on
    ``version_number``.
    """

    @abstractmethod
    async def create_snapshot(
        self,
        *,
        project_id: UUID,
        created_by_user_id: UUID,
        reason: str,
    ) -> ProjectVersion:
        """Capture an immutable snapshot of the project's current state.

        Takes the parent ``projects`` row lock (held for the transaction) so
        the ``MAX(version_number) + 1`` read + insert is race-free (α5d Q6).
        Assembles the canonical snapshot from the live project, its implicit
        default storyboard, and the storyboard's live scenes ordered by
        ``scene_number`` (α5d Q7 — decimals as strings, scene ``id`` preserved
        verbatim for restore round-tripping). Sets ``parent_version_id`` to the
        project's current ``current_version_id`` (``None`` for the first
        snapshot), inserts the row, then advances
        ``projects.current_version_id`` to the new version — which itself
        bumps ``projects.version`` via the guarded row trigger (α5d Q6). An
        empty project (no scenes / no storyboard) is valid: ``scenes`` is
        ``[]`` (α5d Q9). Returns the fully-populated
        :class:`~app.domain.versions.project_version.ProjectVersion`.
        """
        ...

    @abstractmethod
    async def list_by_project(self, project_id: UUID) -> list[ProjectVersionSummary]:
        """Return the project's version history as metadata, newest first.

        Ordered by ``version_number`` DESC (monotonic, so this is newest-first
        by capture time). Returns lightweight
        :class:`~app.domain.versions.project_version.ProjectVersionSummary`
        rows **without** the snapshot / diff blobs (α5d Q4): the list view
        selects only metadata columns so a long history never drags snapshot
        bodies off the database. Not paginated in α5d.1 (a project's version
        count is bounded editorial history).
        """
        ...

    @abstractmethod
    async def get_owned(self, project_id: UUID, version_id: UUID) -> ProjectVersion | None:
        """Return the project's version with ``version_id``, or ``None``.

        Addressed by UUID ``id`` (α5d Q3 — keeps the whole API UUID-addressed);
        ``version_number`` is the user-facing label, not the routing key.
        ``None`` when the version is missing OR belongs to a different project —
        deliberately indistinguishable so
        ``GET /projects/{id}/versions/{version_id}`` maps both to a uniform
        ``404`` (α5d, mirroring α5c D6). Returns the FULL entity including the
        ``snapshot`` blob (this is the detail read).
        """
        ...

    @abstractmethod
    async def restore(
        self,
        *,
        project_id: UUID,
        source_version_id: UUID,
        restored_by_user_id: UUID,
        expected_project_version: int,
    ) -> ProjectVersion | None:
        """Restore ``source_version_id``'s snapshot into live state — α5d.2.

        One transaction, all-or-nothing (pre-flight §3/§9). Under a
        ``SELECT … FOR UPDATE`` lock of the parent ``projects`` row:

        1. **Aggregate OCC fence** — compare the locked ``projects.version``
           against ``expected_project_version`` (the aggregate token the
           client last observed, §4 Aggregate OCC Rule). Mismatch → return
           ``None`` (the use case maps it to ``412``), **no writes**.
        2. **Rewrite the project root** — mutable business columns
           (``name`` / ``description`` / ``duration_seconds`` / ``language`` /
           ``style`` / ``settings``) ← ``snapshot.project``. ``aspect_ratio``
           is immutable (α5b G6): assert-equal, never written (Q2).
        3. **Reconcile scenes by ``id``** across ALL physical rows (live +
           soft-deleted) under the live default storyboard (Q3/Q5): soft-delete
           the removed set first (frees ``scene_number`` slots under the
           partial-unique index), then upsert/insert each snapshot scene with
           its captured ``scene_number`` and full **fat** column set verbatim
           (Q4/§6), reviving soft-deleted rows in place. An empty snapshot
           soft-deletes all live scenes.
        4. **Trailing capture** — append a ``reason=restore`` version whose
           ``parent_version_id`` is ``source_version_id`` (Q7) via the same
           canonical snapshot builder, advancing ``current_version_id`` and
           bumping ``projects.version`` once (Q6).

        The source version was already fetched + project-scoped by the use
        case (``versions.get_owned`` → 404, the 404-before-412 split), so a
        ``None`` here is a *concurrency* outcome (→ 412), never a *visibility*
        one. Returns the new ``reason=restore``
        :class:`~app.domain.versions.project_version.ProjectVersion`.
        """
        ...

    @abstractmethod
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
        """Fork ``source_snapshot`` into a **new independent project** — α5d.3.

        One transaction, all-or-nothing (α5d.3 pre-flight §3, Q1 Option A). The
        source project/version were already fetched + ownership-gated by the use
        case (``projects.get_owned`` + ``versions.get_owned`` → 404); branch
        does **not** touch the source (no OCC fence — the source snapshot is
        immutable), it only *reads* ``source_snapshot`` and *creates* a fresh
        aggregate owned by the caller:

        1. **Create the new project row** — mutable root columns
           (``name`` ← ``new_project_name``, ``description`` /
           ``duration_seconds`` / ``language`` / ``style`` / ``settings`` /
           ``aspect_ratio``) copied from ``source_snapshot['project']``,
           ``current_version_id = NULL``, ``version`` server-default ``1``. A
           live-name collision for this ``(tenant, owner)`` violates
           ``uq_projects_tenant_id_owner_user_id_name`` → ``ConflictError``
           (the use case maps it to ``409``); raised **before** any child rows
           so a rejected branch leaves no debris (§6).
        2. **Materialize scenes** — create the new project's default storyboard,
           then insert each snapshot scene ordered by ``scene_number`` with the
           full **fat** column set (reuses the restore writer), assigning
           **fresh** scene ``id``s (Q5: a new project is a new identity space).
        3. **Seed the ledger** — capture the new project's ``version_number = 1``
           ``reason=branch`` version via the canonical snapshot builder, with a
           structured ``branched_from`` provenance block
           (``{project_id, version_id, version_number}`` of the source, Q3)
           embedded in the snapshot; ``parent_version_id = NULL`` (fresh root).
        4. **Advance the pointer** — set the new project's
           ``current_version_id`` → the v1 row (the guarded row trigger bumps
           the new project's ``version`` to ``2``, exactly like any project's
           first capture — α5d Q6).

        Returns ``(new_project, new_v1)`` — both refreshed to their post-commit
        DB state (the project's ``version`` reflects the pointer-advance bump, so
        the caller's ``ProjectPublic`` reports the correct OCC token). The source
        aggregate is provably unchanged (§4/R... — asserted by the integration
        suite).
        """
        ...


class IRoleRepository(ABC):
    """Persistence surface for ``roles`` + ``roles_users`` join.

    α2a only assigns roles by code; role CRUD (create / delete /
    describe) is admin surface (Phase 3 α6+).
    """

    @abstractmethod
    async def assign_role_by_code(
        self,
        user_id: UUID,
        role_code: str,
        granted_by_user_id: UUID | None = None,
    ) -> None:
        """Attach ``role_code`` to ``user_id``. Idempotent (ON CONFLICT DO NOTHING).

        Raises ``NotFoundError`` if the role code does not exist in the
        ``roles`` lookup table (seeded by migration ``0002``).
        """
        ...


class IRenderJobRepository(ABC):
    """Persistence surface for ``render_jobs``. Introduced by Slice α7.1.

    A **render job** is the request to render a project's timeline and the record
    of that request's lifecycle (RENDER_JOB_AGGREGATE.md / ADR-0039). Unlike the
    α5–α6 domain aggregates it is an *orchestration* aggregate: it owns its own
    status machine and is coordinated purely through its own status + domain
    events (blueprint §7.1 / D9).

    **Ownership is derived through the project** (the ``render_jobs`` row carries
    no ``tenant_id`` / ``owner_user_id``); every method is therefore
    **project-scoped**, and the use case has ALREADY established project ownership
    via :meth:`IProjectRepository.get_owned` before reaching this port — a job
    under another user's project simply returns ``None`` / is omitted
    (anti-enumeration, inherited from α5a/α6.3).

    **Self-versioned aggregate (α7.1 D3.1).** ``render_jobs`` carries its **own**
    ``version`` (``VersionMixin`` + the guarded ``tg_render_jobs_biu_version_bump``
    trigger); the job fences on its own OCC token (the α6.2 ``MediaAsset`` /
    α5a ``Project`` self-versioned pattern), NOT a borrowed timeline token.
    :meth:`cancel` is the version-fenced CAS.

    **No soft-delete.** ``render_jobs`` has no ``deleted_at``; a job is an
    operationally terminal audit record. "Removal" is the ``canceled`` status.
    ``updated_at`` / ``version`` are trigger-owned; the repository hand-sets
    ``version = version + 1`` on the CAS (net +1, mirroring
    :meth:`IProjectRepository.update_owned`).
    """

    @abstractmethod
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
        """Insert a queued render job for ``project_id`` and return it.

        The caller (``CreateRenderJob``) has ALREADY established project ownership
        and that ``timeline_id`` is the project's live timeline. ``id`` /
        timestamps / ``version`` (=1) / ``progress`` (='0.00') are DB-populated;
        worker-owned fields (``started_at`` / ``finished_at`` / ``error`` /
        ``output_media_asset_id`` / ``workflow_run_id``) are left ``NULL``.

        Raises ``ConflictError`` if the ``uq_render_jobs_project_id_idempotency_key``
        uniqueness constraint is violated — i.e. a job with the same
        ``(project_id, idempotency_key)`` already exists. The caller resolves this
        by returning the existing job (α7.1 Q4/D3.7); the constraint is the
        race-safe backstop behind the pre-check.
        """
        ...

    @abstractmethod
    async def get_by_project_and_key(
        self, project_id: UUID, idempotency_key: str
    ) -> RenderJob | None:
        """Return the project's render job with ``idempotency_key``, or ``None``.

        The idempotency pre-check for ``CreateRenderJob`` (α7.1 Q4): a repeat
        create with a key already used for this project returns the existing job
        (``200``) instead of minting a new one. ``None`` when no job with that key
        exists for the project.
        """
        ...

    @abstractmethod
    async def list_by_project(
        self,
        project_id: UUID,
        *,
        status: str | None = None,
    ) -> list[RenderJob]:
        """Return the project's render jobs, newest first, optionally filtered.

        Ordered by ``created_at DESC, id DESC`` (a total order — no duplicate /
        skip under timestamp ties). ``status`` (a ``render_status`` value) narrows
        the result when provided. Project-scoped (the caller established
        ownership). Side-effect-free.
        """
        ...

    @abstractmethod
    async def get_owned(self, project_id: UUID, render_job_id: UUID) -> RenderJob | None:
        """Return the project's render job with ``render_job_id``, or ``None``.

        ``None`` when the job is missing OR belongs to a different project —
        deliberately indistinguishable so ``GET`` maps both to a uniform ``404``
        (α7.1 D3.3, mirroring α5a D5). Addressed by UUID ``id``.
        """
        ...

    @abstractmethod
    async def cancel(
        self,
        project_id: UUID,
        render_job_id: UUID,
        expected_version: int,
    ) -> RenderJob | None:
        """Version-fenced cancel: move ``queued``/``running`` → ``canceled`` (α7.1).

        Atomic CAS: ``UPDATE render_jobs SET status='canceled', version=version+1
        WHERE id=:id AND project_id=:pid AND version=:expected AND status IN
        ('queued','running')``. Returns the canceled :class:`RenderJob`, or
        ``None`` when no row matched — which the use case disambiguates against a
        prior ``get_owned``:

        * job absent / wrong project → already mapped to ``404`` upstream;
        * ``version`` mismatch on a still-cancelable job → ``412`` (D3.5);
        * ``status`` no longer cancelable (``succeeded`` / ``failed``) → the use
          case maps to ``409``; a re-cancel of an already-``canceled`` job is a
          ``200`` no-op resolved upstream (D3.6).

        The ``status`` predicate makes the terminal-state guard race-safe at the
        DB (a worker finishing the job between the use case's read and this CAS
        cannot be silently overwritten). ``version`` is hand-set ``+1`` (net +1
        over the guarded trigger); ``updated_at`` is co-set to ``now()``.
        """
        ...

    # --- Worker-facing lifecycle transitions (α8.4b) ----------------------------
    #
    # These serve the render worker (``RenderWorker.run_once`` → ``ProcessRenderJob``),
    # NOT owner-facing HTTP. They are keyed by ``render_job_id`` alone (the worker
    # already holds the scanned job) and each is a race-safe CAS with a status
    # predicate + hand-set ``version = version + 1`` — mirroring :meth:`cancel`.
    # Additive and outside the ADR-0042 frozen orchestration core (the render path
    # is a downstream Timeline→media transform; invariants W8.4b.1 / W8.4b.2).

    @abstractmethod
    async def list_claimable(self, *, limit: int) -> list[RenderJob]:
        """Return ``queued`` jobs across all projects, oldest first, capped at ``limit``.

        The α8.4b poll ingress (mirrors ``IWorkflowRunRepository.list_paused``):
        ordered ``created_at ASC, id ASC`` (a total order — FIFO, no skip/dup under
        ties). NOT project-scoped — the worker is a server-side consumer that then
        settles each job under its own ``render_job:<id>`` lease. Side-effect-free.
        """
        ...

    @abstractmethod
    async def mark_running(self, render_job_id: UUID) -> RenderJob | None:
        """Version-fenced claim: ``queued`` → ``running`` (sets ``started_at``).

        Atomic CAS: ``UPDATE … SET status='running', started_at=now(),
        version=version+1 WHERE id=:id AND status='queued'``. Returns the running
        job, or ``None`` when no row matched (already claimed/terminal/canceled) —
        the worker treats ``None`` as "another worker owns it" and skips.
        """
        ...

    @abstractmethod
    async def mark_succeeded(
        self,
        render_job_id: UUID,
        *,
        output_media_asset_id: UUID,
        progress: str = "100.00",
    ) -> RenderJob | None:
        """Version-fenced finish: ``running`` → ``succeeded`` with the output asset.

        Atomic CAS: ``… SET status='succeeded', finished_at=now(),
        output_media_asset_id=:asset, progress=:progress, version=version+1
        WHERE id=:id AND status='running'``. Returns the settled job, or ``None``
        when no row matched (e.g. canceled mid-render). ``output_media_asset_id`` is
        the registered render-output ``MediaAsset``.
        """
        ...

    @abstractmethod
    async def mark_failed(
        self,
        render_job_id: UUID,
        *,
        error: dict[str, object],
    ) -> RenderJob | None:
        """Version-fenced finish: ``running`` → ``failed`` with a structured error.

        Atomic CAS: ``… SET status='failed', finished_at=now(), error=:error,
        version=version+1 WHERE id=:id AND status='running'``. Returns the settled
        job, or ``None`` when no row matched. ``error`` is a neutral dict (no
        provider/orchestration detail; W8.4b.2).
        """
        ...


class IExportJobRepository(ABC):
    """Persistence surface for ``export_jobs``. Introduced by Slice α8.5a.

    An **export job** is a user's request to transcode a completed render's master
    ``MediaAsset`` into one delivery encoding ``(format, quality, orientation)`` — strictly
    downstream of render/enrichment (W8.5.1). The master render is canonical; exports are
    replaceable delivery artifacts (W8.5.3).

    **Ownership is derived through the render job → project** (``export_jobs`` carries
    ``requested_by_user_id`` but no ``project_id`` / ``tenant_id``); the owner-facing methods
    are project-scoped, and the caller has ALREADY established project ownership before
    reaching this port (anti-enumeration, inherited from α5a/α7.1). Ownership resolves via
    ``render_job_id → render_jobs.project_id``.

    **Self-versioned aggregate.** ``export_jobs`` carries its own ``version``
    (``VersionMixin``); the worker-facing CAS transitions hand-set ``version = version + 1``
    over the guarded bump trigger (net +1), mirroring :class:`IRenderJobRepository`.

    **Idempotency backstop.** :meth:`add` maps the partial-unique
    ``uq_export_jobs_render_job_id_format_quality_orientation`` violation (ADR-0030 W1.1 —
    over ``status IN ('queued','running','succeeded')``) to ``ConflictError``; the use case
    resolves it by returning the existing active/fulfilled job (α8.5a Fork E).

    **No ``error`` / ``started_at`` columns** (unlike ``render_jobs``): a failed export
    records ``status='failed'`` + ``finished_at`` only; the reason lives in logs + the
    ``ExportJobFailed`` event.
    """

    @abstractmethod
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
        """Insert a queued export job and return it.

        The caller (``CreateExportJob``) has ALREADY established project ownership of the
        referenced render job and validated it is ``succeeded`` with a master output. ``id`` /
        timestamps / ``version`` (=1) / ``download_count`` (=0) are DB-populated; worker-owned
        fields (``finished_at`` / ``output_media_asset_id`` / ``file_size_bytes``) are ``NULL``.

        Raises ``ConflictError`` if the partial-unique
        ``uq_export_jobs_render_job_id_format_quality_orientation`` constraint is violated —
        i.e. an active/fulfilled export for the same ``(render_job_id, format, quality,
        orientation)`` already exists; the use case returns that existing job (Fork E).
        """
        ...

    @abstractmethod
    async def get_active(
        self,
        render_job_id: UUID,
        *,
        format: str,
        quality: str,
        orientation: str,
    ) -> ExportJob | None:
        """Return the active-or-fulfilled export for the tuple, or ``None``.

        The idempotency pre-check / race-recovery lookup for ``CreateExportJob`` (Fork E):
        matches ``status IN ('queued','running','succeeded')`` (the same predicate as the
        partial-unique index), so a repeat request replays the existing delivery artifact
        instead of minting a duplicate. ``failed`` / ``canceled`` rows are ignored (retry
        after failure is permitted).
        """
        ...

    @abstractmethod
    async def get_owned(self, project_id: UUID, export_job_id: UUID) -> ExportJob | None:
        """Return the export job with ``export_job_id`` under ``project_id``, or ``None``.

        Ownership is resolved by joining ``export_jobs → render_jobs`` and matching
        ``render_jobs.project_id``. ``None`` when the job is missing OR belongs to a
        different project — deliberately indistinguishable so ``GET`` maps both to a uniform
        ``404`` (mirror of α7.1 D3.3).
        """
        ...

    # --- Worker-facing lifecycle transitions (α8.5a) ----------------------------
    #
    # These serve the export worker (``ExportWorker.run_once`` → ``ProcessExportJob``), NOT
    # owner-facing HTTP. Each is a race-safe CAS with a status predicate + hand-set
    # ``version = version + 1`` — mirroring the α8.4b render worker transitions.

    @abstractmethod
    async def list_claimable(self, *, limit: int) -> list[ExportJobClaim]:
        """Return ``queued`` export jobs, oldest first, each with its owning ``project_id``.

        The α8.5a poll ingress (mirrors ``IRenderJobRepository.list_claimable``): ordered
        ``created_at ASC, id ASC`` (FIFO, total order). Joins through ``render_jobs`` to
        resolve ``project_id`` so the worker can settle each job via the project-scoped
        ports. NOT project-scoped — the worker is a server-side consumer. Side-effect-free.
        """
        ...

    @abstractmethod
    async def mark_running(self, export_job_id: UUID) -> ExportJob | None:
        """Version-fenced claim: ``queued`` → ``running``.

        Atomic CAS ``… SET status='running', version=version+1 WHERE id=:id AND
        status='queued'``. Returns the running job, or ``None`` when no row matched (already
        claimed/terminal) — the worker treats ``None`` as "another worker owns it" and skips.
        (``export_jobs`` has no ``started_at`` column, unlike ``render_jobs``.)
        """
        ...

    @abstractmethod
    async def mark_succeeded(
        self,
        export_job_id: UUID,
        *,
        output_media_asset_id: UUID,
        file_size_bytes: int,
    ) -> ExportJob | None:
        """Version-fenced finish: ``running`` → ``succeeded`` with the delivery artifact.

        Atomic CAS ``… SET status='succeeded', finished_at=now(),
        output_media_asset_id=:asset, file_size_bytes=:size, version=version+1 WHERE id=:id
        AND status='running'``. Returns the settled job, or ``None`` when no row matched
        (e.g. canceled mid-export).
        """
        ...

    @abstractmethod
    async def mark_failed(self, export_job_id: UUID) -> ExportJob | None:
        """Version-fenced finish: ``running`` → ``failed``.

        Atomic CAS ``… SET status='failed', finished_at=now(), version=version+1 WHERE
        id=:id AND status='running'``. Returns the settled job, or ``None`` when no row
        matched. ``export_jobs`` has no ``error`` column — the reason lives in the log +
        the ``ExportJobFailed`` event (W8.5.2).
        """
        ...

    # --- Download accounting (α8.5b.1) ------------------------------------------
    #
    # Owner-facing telemetry, NOT a lifecycle transition. Deliberately does NOT bump
    # ``version`` (download counts are metrics, not OCC state — W8.5b.3) and is called
    # best-effort by the download use case: a failure here is telemetry loss, never a
    # user-visible download failure.

    @abstractmethod
    async def record_download(self, export_job_id: UUID) -> ExportJob | None:
        """Increment download telemetry on a ``succeeded`` export (α8.5b.1).

        Atomic ``… SET download_count = download_count + 1, last_downloaded_at = now(),
        updated_at = now() WHERE id=:id AND status='succeeded'``. Returns the updated job, or
        ``None`` when no row matched (missing, or not ``succeeded`` — a non-servable job is
        never counted). Does **not** bump ``version``: this is accounting, not a state change,
        and must never participate in optimistic-concurrency fences (W8.5b.3).
        """
        ...


class IEventOutboxRepository(ABC):
    """Persistence surface for the transactional outbox (``event_outbox``, CR-4).

    Introduced by Slice α7.1 to exercise the outbox pattern (blueprint §6 / D9):
    a domain event is written to ``event_outbox`` **in the same transaction** as
    the state change that produced it, so state + intent-to-publish commit
    atomically. α7.1 only *produces* rows (``RenderJobCreated`` /
    ``RenderJobCanceled``); Slice α7.3 adds the **relay read/mark surface**
    (:meth:`fetch_unpublished` / :meth:`mark_published` / :meth:`mark_failed`) so
    the relay can publish rows and record delivery outcomes. Producers still only
    :meth:`add`; the relay is the sole reader.
    """

    @abstractmethod
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
        """Append one unpublished event row (``published_at`` left ``NULL``).

        Written within the caller's UnitOfWork transaction so it commits with the
        aggregate mutation (atomic state + event). ``aggregate_type`` /
        ``aggregate_id`` identify the source aggregate (e.g. ``"render_job"`` +
        the job id); ``event_type`` is the event name (e.g. ``RenderJobCreated``);
        ``payload`` is the JSON event body; ``occurred_at`` is the domain event
        instant. ``id`` / ``attempts`` (=0) are DB-populated; ``metadata`` defaults
        to ``{}``.
        """
        ...

    @abstractmethod
    async def fetch_unpublished(self, *, limit: int, max_attempts: int) -> list[OutboxEvent]:
        """Claim a batch of unpublished, not-yet-parked events for the relay (α7.3).

        Selects ``published_at IS NULL AND attempts < :max_attempts`` ordered by
        ``occurred_at`` (best-effort chronological, ADR-0041 D9 — never a *total*
        order) with ``FOR UPDATE SKIP LOCKED`` so concurrent relay passes claim
        disjoint batches without blocking. Excluding ``attempts >= max_attempts``
        parks poison rows in place so one bad event cannot head-of-line-block the
        queue (α7.3 sign-off Q3). Must run inside the relay's transaction; the row
        locks are held until that transaction commits.
        """
        ...

    @abstractmethod
    async def mark_published(self, *, event_id: UUID, published_at: datetime) -> None:
        """Stamp ``published_at`` on a successfully-delivered event.

        Called by the relay after :class:`PublisherPort` returns OK, on a row the
        same transaction locked via :meth:`fetch_unpublished`.
        """
        ...

    @abstractmethod
    async def mark_failed(self, *, event_id: UUID, error: str) -> None:
        """Record a failed delivery: ``attempts += 1``, set ``last_error``.

        Leaves ``published_at`` NULL so the event is retried on a later pass
        (at-least-once). Once ``attempts`` reaches the relay's ``max_attempts`` the
        row is parked (excluded by :meth:`fetch_unpublished`) — no DLQ table, no
        scheduler (α7.3 sign-off Q3).
        """
        ...


class IWorkflowRunRepository(ABC):
    """Persistence surface for ``workflow_runs`` (+ steps + checkpoints). Slice α7.2.

    A **workflow run** is the record of one workflow execution and the orchestration
    graph beneath it — ordered ``workflow_steps`` and append-only
    ``workflow_checkpoints`` (WORKFLOW_RUN_AGGREGATE.md / ADR-0040). Like
    ``RenderJob`` it is an *orchestration* aggregate that owns its own status
    machine and coordinates through its status + outbox events (D9), and its
    **ownership is derived through the project** (no ``tenant_id`` / ``owner_user_id``
    column) — every method is **project-scoped** and the use case has ALREADY
    established project ownership before reaching this port.

    **Status-guarded CAS, no version token (α7.2 D3.2).** Neither ``workflow_runs``
    nor ``workflow_steps`` carries a ``version`` column (they are not in
    ``_VERSION_BUMP_TABLES``). Every lifecycle transition is therefore a
    **status-predicated** ``UPDATE … WHERE status IN (<allowed_from>)`` compare-and-
    swap (returns the row on success, ``None`` when the guard did not match — the
    use case re-classifies to ``404`` / ``409`` / idempotent ``200``). Non-transition
    metadata is last-writer-wins. This is the workflow-specific concurrency model —
    a documented divergence from ``RenderJob``'s version-fenced cancel.

    **Checkpoints are append-only (ADR-0014)** — :meth:`append_checkpoint` inserts;
    the DB ``reject_mutation`` trigger blocks UPDATE/DELETE. **No soft-delete** — a
    run is a terminal audit record; "removal" is the ``canceled`` status.
    """

    # ---- create + seed -------------------------------------------------

    @abstractmethod
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
        """Insert a ``queued`` workflow run for ``project_id`` and return it.

        The caller (``CreateWorkflowRun``) has ALREADY established project ownership
        and resolved ``workflow_key@workflow_version`` against the registry. ``id`` /
        timestamps are DB-populated; ``started_at`` / ``finished_at`` /
        ``output_summary`` / ``error`` are left ``NULL``. Steps are seeded separately
        via :meth:`seed_steps`.

        Raises ``ConflictError`` on the ``uq_workflow_runs_project_id_idempotency_key``
        violation — the caller resolves it by returning the existing run (Q7, α7.1
        parity); the constraint is the race-safe backstop behind the pre-check.
        """
        ...

    @abstractmethod
    async def seed_steps(
        self, workflow_run_id: UUID, steps: list[tuple[int, str]]
    ) -> list[WorkflowStep]:
        """Bulk-insert the run's ``pending`` steps from ``(step_index, step_name)`` pairs.

        Called once, immediately after :meth:`add`, from the same transaction. The
        ``uq_workflow_steps_workflow_run_id_step_index`` uniqueness makes seeding
        resume-safe (never duplicates a step). Returns the seeded steps in
        ``step_index`` order.
        """
        ...

    # ---- reads ---------------------------------------------------------

    @abstractmethod
    async def get_by_project_and_key(
        self, project_id: UUID, idempotency_key: str
    ) -> WorkflowRun | None:
        """Return the project's run with ``idempotency_key``, or ``None`` (Q7 pre-check)."""
        ...

    @abstractmethod
    async def list_by_project(
        self, project_id: UUID, *, status: str | None = None
    ) -> list[WorkflowRun]:
        """Return the project's runs, newest first (``created_at DESC, id DESC``), optionally by status."""
        ...

    @abstractmethod
    async def get_owned(self, project_id: UUID, workflow_run_id: UUID) -> WorkflowRun | None:
        """Return the project's run with ``workflow_run_id``, or ``None`` (uniform ``404`` upstream)."""
        ...

    @abstractmethod
    async def list_steps(self, workflow_run_id: UUID) -> list[WorkflowStep]:
        """Return the run's steps in ``step_index`` order."""
        ...

    @abstractmethod
    async def latest_checkpoint(
        self, workflow_run_id: UUID, step_index: int | None = None
    ) -> WorkflowCheckpoint | None:
        """Return the most recent checkpoint for the run (or a given ``step_index``), or ``None``.

        Ordered by ``created_at DESC, id DESC``. Used by the runner to obtain a
        step's resume state (the preceding step's latest checkpoint).
        """
        ...

    # ---- run transitions (status-guarded CAS) --------------------------

    @abstractmethod
    async def mark_run_running(self, workflow_run_id: UUID) -> WorkflowRun | None:
        """CAS ``queued → running`` (sets ``started_at``). ``None`` if not ``queued``."""
        ...

    @abstractmethod
    async def mark_run_succeeded(
        self, workflow_run_id: UUID, output_summary: dict[str, Any]
    ) -> WorkflowRun | None:
        """CAS ``running → succeeded`` (sets ``output_summary`` + ``finished_at``). ``None`` if not ``running``."""
        ...

    @abstractmethod
    async def mark_run_failed(
        self, workflow_run_id: UUID, error: dict[str, Any]
    ) -> WorkflowRun | None:
        """CAS ``running → failed`` (sets ``error`` + ``finished_at``). ``None`` if not ``running``."""
        ...

    @abstractmethod
    async def mark_run_paused(self, workflow_run_id: UUID) -> WorkflowRun | None:
        """CAS ``running → paused`` for an async ``IN_PROGRESS`` command (α7.6, Q2).

        ``paused`` is **not terminal** — ``finished_at`` is left unset; the α8.3
        completion service resumes the run under the checkpointed ``provider_job_id``.
        Returns the paused run, or ``None`` if the run was not ``running``.
        """
        ...

    @abstractmethod
    async def resume_run(self, workflow_run_id: UUID) -> WorkflowRun | None:
        """CAS ``paused → running`` — the α8.3 completion resume seam.

        The inverse of :meth:`mark_run_paused`: an async provider job has resolved, so
        ``ResumeWorkflowRun`` takes the run back to ``running`` (leaving ``started_at``
        as-is, ``finished_at`` still unset) before recording terminal usage, completing
        the paused step, and driving the run to a terminal state. Status-guarded so a
        concurrent resume (poll racing a future webhook) that already moved the run out
        of ``paused`` yields ``None`` — the completion is a no-op replay. Returns the
        running run, or ``None`` if the run was not ``paused``.
        """
        ...

    @abstractmethod
    async def list_paused(self) -> list[WorkflowRun]:
        """Return all ``paused`` runs (oldest first) — the α8.3 polling-ingress scan.

        Global (not project-scoped): the completion poller enumerates every paused run
        to resolve its in-flight provider job. Ordered by ``created_at ASC, id ASC`` so
        the oldest pause is polled first.
        """
        ...

    @abstractmethod
    async def find_paused_by_provider_job_id(self, provider_job_id: str) -> WorkflowRun | None:
        """Return the ``paused`` run whose checkpoint carries ``provider_job_id``, or ``None``.

        An **implementation detail of the repository** (α8.3b), *not* a new
        architectural contract: it merely resolves the webhook's only trusted datum
        (the provider job id) to the run that is paused on it, by matching the
        ``_paused.provider_job_id`` field persisted in the latest checkpoint. Used by
        the webhook ingress to obtain ``(project_id, id)`` for the frozen
        ``CompletionEngine.complete()``. Returns ``None`` when no *paused* run matches
        (unknown job id, or already resumed/terminal) — an idempotent no-op for the
        caller.
        """
        ...

    @abstractmethod
    async def cancel(self, project_id: UUID, workflow_run_id: UUID) -> WorkflowRun | None:
        """Status-guarded cancel: ``{queued,running,paused} → canceled`` (sets ``finished_at``).

        ``UPDATE … WHERE id=? AND project_id=? AND status IN
        ('queued','running','paused')``. Returns the canceled run, or ``None`` when
        no row matched — the use case re-classifies against a prior ``get_owned``
        (already ``canceled`` → ``200`` no-op; ``succeeded``/``failed`` → ``409``).
        There is **no** version fence (no token exists — D3.2/D3.7).
        """
        ...

    # ---- step transitions (status-guarded CAS) -------------------------

    @abstractmethod
    async def mark_step_running(
        self, workflow_run_id: UUID, step_index: int
    ) -> WorkflowStep | None:
        """CAS ``{pending,retrying} → running`` (sets ``started_at``). ``None`` if not runnable."""
        ...

    @abstractmethod
    async def mark_step_succeeded(
        self, workflow_run_id: UUID, step_index: int, output: dict[str, Any]
    ) -> WorkflowStep | None:
        """CAS ``running → succeeded`` (sets ``output`` + ``finished_at``). ``None`` if not ``running``."""
        ...

    @abstractmethod
    async def mark_step_retrying(
        self, workflow_run_id: UUID, step_index: int, error: dict[str, Any]
    ) -> WorkflowStep | None:
        """CAS ``running → retrying`` (``retries = retries + 1``, sets ``error``). ``None`` if not ``running``."""
        ...

    @abstractmethod
    async def mark_step_failed(
        self, workflow_run_id: UUID, step_index: int, error: dict[str, Any]
    ) -> WorkflowStep | None:
        """CAS ``running → failed`` (sets ``error`` + ``finished_at``). ``None`` if not ``running``."""
        ...

    # ---- checkpoints (append-only) -------------------------------------

    @abstractmethod
    async def append_checkpoint(
        self, workflow_run_id: UUID, step_index: int, state: dict[str, Any]
    ) -> WorkflowCheckpoint:
        """Append one immutable resume point (ADR-0014). Never updates an existing row."""
        ...


class IProviderSettingsRepository(ABC):
    """Read-only surface for ``provider_settings`` (Slice α7.4, minimal read path).

    Per the α7.4 sign-off (Q4) this ships **read-only** and resolves a single
    ``(provider, key)`` value with **tenant-shadows-global** precedence — the seam
    a later slice builds "load enabled providers → select configured provider →
    construct adapter" on. There is **no** fallback / weighting / priority /
    health-ordering here (those arrive once multiple real providers exist), and no
    write surface (config authoring is a separate concern).
    """

    @abstractmethod
    async def get_value(
        self, provider: str, key: str, tenant_id: UUID | None = None
    ) -> Mapping[str, Any] | None:
        """Return the JSON value for ``(provider, key)``, or ``None`` if unset.

        When ``tenant_id`` is given, a tenant-scoped row shadows the global
        (``tenant_id IS NULL``) row for the same ``(provider, key)``; the global row
        is the fallback. Secret values are returned verbatim — masking, if any, is
        the caller's concern (α7.4 has no secret consumers).
        """
        ...


class IUsageRecordRepository(ABC):
    """Write/read surface for ``usage_records`` (Slice α7.5, ADR-0019 / ADR-0033).

    The Usage Recorder's only aggregate-free persistence target (W7.5.1). Writes
    are **append-only** (one immutable row per call) and **idempotent** on
    ``request_id`` via the per-partition ``uq_<child>_request_id`` partial-unique
    (ADR-0033): a colliding insert raises :class:`DuplicateRequestIdError`, which
    the recorder recovers from by returning the pre-existing row.
    """

    @abstractmethod
    async def insert(self, new: NewUsageRecord) -> UsageRecordRow:
        """Insert one usage row (SAVEPOINT-guarded) and return it.

        Raises :class:`app.application.interfaces.usage_recorder.DuplicateRequestIdError`
        when ``new.request_id`` is non-NULL and a row already exists for it (the
        ADR-0033 unique fired). A NULL ``request_id`` always inserts (system-initiated
        calls coexist — ADR-0033 partial predicate ``WHERE request_id IS NOT NULL``).
        """
        ...

    @abstractmethod
    async def get_by_request_id(self, request_id: str) -> UsageRecordRow | None:
        """Return the existing row for ``request_id`` (most recent), or ``None``."""
        ...


class IModelPricingRepository(ABC):
    """Read-only ``ai_model_pricing`` resolver (Slice α7.5, CR-11).

    Resolves the price row effective at a point in time for one ``(model_id,
    unit)``. Read-only — the recorder observes pricing, it never authors it.
    """

    @abstractmethod
    async def get_effective(
        self, *, model_id: UUID, unit: str, at: datetime
    ) -> EffectivePrice | None:
        """Return the pricing row effective at ``at`` for ``(model_id, unit)``.

        Prefers the row whose ``[effective_from, effective_to)`` window contains
        ``at`` (open-ended when ``effective_to IS NULL``). ``None`` when no pricing
        is configured — the recorder then prices that unit at 0 and warns (Q5).
        """
        ...
