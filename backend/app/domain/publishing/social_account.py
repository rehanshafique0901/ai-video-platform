"""``SocialAccount`` aggregate — a user's connected external destination (α8.6a).

The publishing bounded context's identity aggregate. A ``SocialAccount`` records
**which external channel a user may publish to** (e.g. a YouTube channel) together with
its connection lifecycle (``connected`` → ``expired`` / ``revoked``). It is deliberately
distinct from the login-identity ``oauth_identities`` seam (ADR-0047 C1): that answers
"who is this user?"; this answers "which destination, with what authorization?".

**The secret never lives here.** The OAuth access / refresh tokens are owned by the
credential context (``social_credentials`` + the credential service, C1/C7); this
aggregate carries only non-secret profile + status. Frozen for value-semantics, mirroring
:class:`app.domain.identity.user.User` and :class:`app.domain.export.export_job.ExportJob`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class AccountStatus(StrEnum):
    """Lifecycle of a connected destination account.

    ``CONNECTED`` — a usable credential is stored. ``EXPIRED`` — the credential lapsed and
    could not be refreshed. ``REVOKED`` — the user (or the platform) invalidated it; the
    stored credential has been removed (C6). Only ``CONNECTED`` accounts can yield an
    :class:`~app.application.interfaces.social_credential_store.AuthorizedContext`.
    """

    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class SocialAccount:
    """One connected external destination — a row of the ``social_accounts`` table.

    ``platform`` is a free-text destination key (``youtube`` / ``mock``); a platform enum
    or catalogue is deferred until multiple real destinations justify it (OQ2). ``scopes``
    are the non-secret granted OAuth scopes. Multiple accounts may exist per
    ``(user_id, platform)`` — uniqueness is ``(user_id, platform, external_account_id)`` (R4).
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    platform: str
    external_account_id: str
    display_name: str | None
    status: AccountStatus
    scopes: tuple[str, ...]
    connected_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_connected(self) -> bool:
        """True iff the account is in the ``CONNECTED`` state (a credential should exist)."""
        return self.status is AccountStatus.CONNECTED


__all__ = ["AccountStatus", "SocialAccount"]
