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

from app.domain.identity.session import Session
from app.domain.identity.tenant import Tenant
from app.domain.identity.user import User
from app.domain.projects.project import Project


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
