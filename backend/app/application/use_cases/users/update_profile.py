"""``UpdateUserProfile`` use case (Slice α4).

Contract (API_CONTRACT §3.x — added by α4):

    PATCH /api/v1/users/me
      body:    { display_name, version }
      → 200    { data: UserPublic (with incremented version), meta }
      → 412    { error: { code: VERSION_CONFLICT, ... } }
      → 401    { error: { code: UNAUTHENTICATED, ... } }  (via CurrentUserDep)
      → 422    { error: { code: VALIDATION_FAILED, ... } } (via DTO)

This use case is the reference implementation of the α4 "canonical
authenticated mutation flow" (pre-flight §10 exit criteria + reviewer
refinement R4; see also ``docs/api/AUTH_ENDPOINTS.md`` §8):

    1. Authenticate via ``CurrentUserDep``           (the API layer)
    2. Validate request DTO                          (the API layer)
    3. Check optimistic concurrency (``version``)    (this use case)
    4. Apply domain mutation                         (this use case)
    5. Persist via a targeted repository CAS         (this use case)
    6. Return the updated representation             (this use case)

Version-fence semantics (pre-flight §D6a):

``users.version`` increments **only when at least one persisted
field actually changes**. Same-value PATCHes (e.g. the client sending
back the same ``display_name`` it just read) are treated as
successful no-ops — the response carries the *unchanged* row and
version. No write hits the DB; no dirty replication log; no
``updated_at`` bump. The wire response is byte-identical to a real
change so the client's next PATCH still succeeds without a manual
GET-refresh.

Anti-enumeration (pre-flight §A10): the repository collapses the two
distinct internal failures ("version mismatch" and "user soft-deleted
between auth and this write") into a single ``None`` return, and this
use case surfaces both as one :class:`VersionConflictError`. A client
MUST NOT be able to distinguish a stale ``version`` value from an
already-deleted account — both are the same 412 response.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import VersionConflictError
from app.domain.identity.user import User

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UpdateUserProfileResult:
    """Outcome of a successful ``UpdateUserProfile.execute``.

    ``changed`` distinguishes the two success paths:

    * ``True`` — the persisted row was updated. ``user.version`` is
      one greater than the caller-provided ``expected_version`` and
      ``user.updated_at`` was bumped to server time.
    * ``False`` — same-value no-op. ``user.version`` equals the
      caller-provided ``expected_version`` and ``user.updated_at`` is
      unchanged from what the caller last read. Wire response looks
      identical to ``changed=True``; only the server-side log records
      the distinction.

    The API layer does not need to inspect ``changed`` — it returns
    ``user`` to the client either way. This field exists so callers
    (tests, future admin/audit surfaces) can observe the no-op path
    without parsing log lines.
    """

    user: User
    changed: bool


class UpdateUserProfile:
    """Update the authenticated caller's profile with a version fence.

    α4 shipping surface: ``display_name`` only. Later slices adding
    more patchable fields extend the ``execute`` signature with
    keyword-only optional args rather than introducing a parallel use
    case — the version-fence orchestration is what deserves to be
    shared.
    """

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        user_id: UUID,
        expected_version: int,
        display_name: str,
        ip: str | None = None,
    ) -> UpdateUserProfileResult:
        async with self._uow:
            updated = await self._uow.users.update_profile(
                user_id=user_id,
                expected_version=expected_version,
                display_name=display_name,
            )

            if updated is None:
                # Version mismatch OR user gone — indistinguishable by
                # design. Log the reason at WARN for operational
                # visibility; the client sees only the generic 412.
                # ``current_version`` is deliberately absent from the
                # log payload — the repository does not disclose it,
                # and re-fetching just to log it would race against
                # concurrent writers anyway (α4 §A11 "only when
                # known" clause).
                _LOGGER.warning(
                    "user.profile.update_rejected",
                    reason="version_mismatch",
                    user_id=str(user_id),
                    expected_version=expected_version,
                    ip=ip,
                )
                raise VersionConflictError("Resource has been modified.")

            await self._uow.commit()

        # The repository returns the same-value no-op case by echoing
        # the pre-update row (version unchanged). This is the single
        # place where the API/use-case boundary distinguishes the two
        # success paths — everywhere below, ``changed`` is authoritative.
        changed = updated.version != expected_version

        if changed:
            _LOGGER.info(
                "user.profile.updated",
                user_id=str(updated.id),
                changed_fields=["display_name"],
                previous_version=expected_version,
                new_version=updated.version,
                ip=ip,
            )
        else:
            # Same-value no-op — INFO not WARN (α4 §A11 log-level
            # table). This branch is deliberately not
            # anti-enumeration-sensitive: the client is asking us to
            # store what we already have, so there's nothing to leak.
            _LOGGER.info(
                "user.profile.update_rejected",
                reason="same_value_noop",
                user_id=str(updated.id),
                ip=ip,
            )

        return UpdateUserProfileResult(user=updated, changed=changed)
