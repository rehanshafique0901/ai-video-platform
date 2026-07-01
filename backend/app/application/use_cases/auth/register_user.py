"""``RegisterUser`` use case (Slice α2a).

Contract (API_CONTRACT §3.1):

    POST /auth/register
      body: { email, password, name }
      → 201 { user, access_token, refresh_token }

Steps, all inside one UoW:

0. **Application-layer email-uniqueness pre-check.** Because Decision
   1A auto-creates a fresh tenant per signup, the DB's per-tenant
   ``uq_users_tenant_id_email`` unique constraint cannot catch the
   "same email registered twice" scenario — each signup arrives at a
   different ``tenant_id`` so the constraint sees a distinct pair
   every time. Without this pre-check, a re-registration would
   silently create a second account under the same email in a second
   tenant, which is both a UX bug (user cannot tell which password
   goes with which workspace) and a phishing vector once invitation
   flows land. Race window between the pre-check and the tenant
   insert is small in practice and acceptable for α2a; a later
   hardening pass may introduce an application-level lock table
   keyed on email or a rate-limit if this proves exploitable.
1. Derive a candidate tenant ``slug`` from the email local-part; if
   the DB unique constraint rejects it, retry with a fresh random
   suffix up to ``_MAX_SLUG_ATTEMPTS`` times.
2. Insert ``Tenant`` row (plan_tier='free', owner-model per Decision 1A).
3. Insert ``User`` row (email lowercased upstream, Argon2id hash).
4. Assign the ``owner`` role via ``RoleRepository`` (the seed
   migration ``0002_seed_system_data`` populates the ``roles`` table
   with workspace-permission codes: ``owner, admin, editor, viewer,
   billing, support``). The α2a pre-flight originally called for
   ``user + owner`` on the assumption that ``user`` was a baseline
   workspace role; on implementation this proved incorrect — the
   ``user`` name lives on the ``auth_role`` ENUM
   (``schema.md`` §0.1, a plan-tier concept), NOT in the ``roles``
   lookup table. Assigning ``owner`` alone captures the intent:
   the creator owns the tenant they just created. "Any authenticated
   user" is enforced by JWT validity, not by a role row.
5. Issue a login-shape ``IssuedTokens`` bundle (fresh family + sid).
6. Insert the corresponding ``sessions`` row.
7. Commit the UoW.

The returned ``RegisterUserResult`` carries the domain entities +
raw tokens so the router can shape the JSON response without
knowing about JWT internals.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import structlog

from app.application.interfaces.security import IPasswordHasher, IssuedTokens, ITokenIssuer
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.errors import (
    EmailAlreadyRegisteredError,
    TenantSlugCollisionError,
)
from app.core.errors import ConflictError
from app.domain.identity.session import Session
from app.domain.identity.tenant import Tenant
from app.domain.identity.user import User

_LOGGER = structlog.get_logger(__name__)

# 3 attempts is enough to make collisions astronomically unlikely
# (email-derived slug + 6-char random suffix = ~2 billion candidates
# per email); more than that indicates a real bug or a hostile actor.
_MAX_SLUG_ATTEMPTS = 3

# Only ``[a-z0-9-]`` — everything else in a URL slug is either a
# hostname parser edge case or a foot-gun for future routing.
_SLUG_ALLOWED = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True, slots=True)
class RegisterUserResult:
    user: User
    tenant: Tenant
    session: Session
    tokens: IssuedTokens


class RegisterUser:
    """Register a self-service user + auto-create their owner tenant."""

    def __init__(
        self,
        uow: IUnitOfWork,
        hasher: IPasswordHasher,
        token_issuer: ITokenIssuer,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._token_issuer = token_issuer

    async def execute(
        self,
        email: str,
        password: str,
        name: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> RegisterUserResult:
        password_hash = self._hasher.hash(password)

        async with self._uow:
            # Step 0 — global email-uniqueness pre-check. See class
            # docstring for the rationale (auto-created-tenant defeats
            # the DB per-tenant uniqueness constraint).
            existing = await self._uow.users.get_by_email(email)
            if existing is not None:
                _LOGGER.warning(
                    "auth.register.conflict",
                    email_domain=_email_domain(email),
                    existing_user_id=str(existing.id),
                )
                raise EmailAlreadyRegisteredError(
                    "email already registered",
                    details={"email_domain": _email_domain(email)},
                )

            tenant = await self._create_tenant_with_retry(name)

            user_id = uuid4()
            try:
                persisted_user = await self._uow.users.add(
                    User(
                        id=user_id,
                        tenant_id=tenant.id,
                        email=email,
                        password_hash=password_hash,
                        display_name=name,
                        email_verified_at=None,
                        last_login_at=None,
                        # DB defaults populate these on flush.
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                        version=1,
                    )
                )
            except ConflictError as e:
                _LOGGER.warning(
                    "auth.register.conflict",
                    email_domain=_email_domain(email),
                    tenant_id=str(tenant.id),
                )
                raise EmailAlreadyRegisteredError(
                    "email already registered",
                    details=e.details,
                ) from e

            # Assign the ``owner`` role — the tenant-level administrative
            # authority per ``schema.md`` §5. See the class docstring for
            # why we do not also assign ``user`` (that name lives on the
            # ``auth_role`` ENUM, not the ``roles`` lookup table).
            await self._uow.roles.assign_role_by_code(user_id=persisted_user.id, role_code="owner")

            tokens = self._token_issuer.issue_for_login(persisted_user)

            session = await self._uow.sessions.add(
                Session(
                    id=tokens.session_id,
                    user_id=persisted_user.id,
                    family_id=tokens.family_id,
                    token_hash=tokens.refresh_token_hash,
                    ip=ip,
                    user_agent=user_agent,
                    issued_at=tokens.issued_at,
                    last_used_at=tokens.issued_at,
                    expires_at=tokens.refresh_expires_at,
                    revoked_at=None,
                )
            )

            await self._uow.commit()

        _LOGGER.info(
            "auth.register.succeeded",
            user_id=str(persisted_user.id),
            tenant_id=str(tenant.id),
            session_id=str(session.id),
        )
        return RegisterUserResult(
            user=persisted_user, tenant=tenant, session=session, tokens=tokens
        )

    async def _create_tenant_with_retry(self, display_name: str) -> Tenant:
        """Insert a Tenant, retrying on slug collision with fresh random suffixes.

        Each attempt runs inside a SAVEPOINT (see ``TenantRepository.add``)
        so a collision rolls back only that attempt; the outer UoW
        transaction stays alive for the subsequent user / role / session
        inserts.
        """
        base_slug = _slugify(display_name)
        last_error: ConflictError | None = None
        for _ in range(_MAX_SLUG_ATTEMPTS):
            # ``token_hex(3)`` = 6 lowercase hex chars — ~16 million values,
            # so collisions per email are astronomically rare.
            suffix = secrets.token_hex(3)
            candidate = f"{base_slug}-{suffix}"[:255]
            try:
                return await self._uow.tenants.add(
                    Tenant(
                        id=uuid4(),
                        name=display_name.strip() or "Workspace",
                        slug=candidate,
                        plan_tier="free",
                        # DB defaults populate these on flush.
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            except ConflictError as e:
                last_error = e
                continue
        raise TenantSlugCollisionError(
            "could not allocate a unique tenant slug",
            details={"base_slug": base_slug, "attempts": _MAX_SLUG_ATTEMPTS},
        ) from last_error


def _slugify(text: str) -> str:
    """Lowercase, ASCII-only, hyphen-separated. Non-alphanumeric → hyphen; collapsed."""
    lowered = text.strip().lower()
    ascii_ish = _SLUG_ALLOWED.sub("-", lowered)
    collapsed = re.sub(r"-+", "-", ascii_ish).strip("-")
    return collapsed or "workspace"


def _email_domain(email: str) -> str:
    """Return everything after the last ``@`` in ``email``, or an empty string.

    Used for structured logs — the local-part is PII and must not
    appear in logs; the domain is safe.
    """
    _, _, domain = email.rpartition("@")
    return domain
