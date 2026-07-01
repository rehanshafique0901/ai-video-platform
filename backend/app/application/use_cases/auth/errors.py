"""Domain-level auth errors, expressed as ``ApplicationError`` subclasses.

Two design choices worth calling out:

1. **These extend ``app.core.errors.ApplicationError``** rather than
   defining a parallel domain hierarchy translated by the router. The
   FastAPI exception handler in ``app.core.errors`` already maps every
   ``ApplicationError`` subclass to the API_CONTRACT §1.2 envelope, so
   piggy-backing on that machinery is much less code than a separate
   translation layer — and the import direction is still legal
   (application → core, never api or infrastructure).

2. **``InvalidCredentialsError`` is a single exception raised for
   *both* "unknown email" and "wrong password"**, with a deliberately
   vague message. This is anti-enumeration hardening (OWASP ASVS L2 §
   2.6.3): a client MUST NOT be able to distinguish an unregistered
   email from a wrong password. Server-side logs still emit a
   ``reason`` field on ``auth.login.failed`` for operational visibility.
"""

from __future__ import annotations

from app.core.errors import ConflictError, UnauthorizedError


class EmailAlreadyRegisteredError(ConflictError):
    """The ``(tenant_id, email)`` uniqueness constraint was violated."""


class TenantSlugCollisionError(ConflictError):
    """Could not generate a unique tenant slug after the retry budget."""


class InvalidCredentialsError(UnauthorizedError):
    """Login failed. Emit the same message for unknown-email + wrong-password."""


class InvalidRefreshTokenError(UnauthorizedError):
    """Refresh / logout failed. One message for every failure mode.

    Introduced in α2b. Refresh has multiple internal failure modes
    (signature invalid, expired, hash-miss, sid mismatch, reuse
    detected, user gone). Each one gets a distinct server-side
    ``auth.refresh.rejected`` / ``auth.refresh.reuse_detected`` log
    event with the actual reason, but every one raises this single
    error type so the client-facing envelope is byte-identical.
    A client MUST NOT be able to distinguish an unknown token from a
    revoked-family-member replay (OWASP ASVS L2 §3.5).
    """
