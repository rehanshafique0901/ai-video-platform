"""Ports: repository ABCs.

Slice α1 shipped ``IUserRepository`` with two read-only smoke methods
(``count`` / ``exists_by_id``). Slice α2a extends the interface with
entity-returning queries (``get_by_email`` / ``get_by_id``), mutation
methods (``add`` / ``update_last_login``), and introduces the three
sibling ports needed for register + login:

* ``ITenantRepository`` — schema §1
* ``ISessionRepository`` — schema §4 (α2a needs ``add`` only; α2b
  extends with ``get_by_hash`` / ``revoke`` / ``list_family``)
* ``IRoleRepository``   — schema §5 (α2a needs ``assign_role_by_code``)

Per the approved review: repositories answer persistence questions
only. Orchestration (token rotation, replay detection, family
revocation) lives in the use cases, not in these interfaces.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from app.domain.identity.session import Session
from app.domain.identity.tenant import Tenant
from app.domain.identity.user import User


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
    """Persistence surface for ``sessions`` (α2a: insert-only).

    α2b extends this interface with ``get_by_hash``, ``revoke``, and
    ``list_family``. Rotation and replay detection are use-case
    concerns; the port stays narrow.
    """

    @abstractmethod
    async def add(self, session: Session) -> Session:
        """Insert a new sessions row and return the persisted entity."""
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
