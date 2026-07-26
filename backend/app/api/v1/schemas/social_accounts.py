"""DTOs for ``/api/v1/social-accounts/*`` (α8.6a Account Connections).

Deliberate field selection (same discipline as ``schemas/notifications.py``): only
non-secret profile is exposed. **No token, refresh token, or credential field is ever
declared here** (C8) — the wire contract cannot leak credential material even if the domain
grew one.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.publishing.social_account import AccountStatus


class ConnectSocialAccountRequest(BaseModel):
    """``POST /social-accounts/connect`` body — which destination to connect."""

    platform: str = Field(min_length=1, max_length=64)


class ConnectSocialAccountResponse(BaseModel):
    """``POST /social-accounts/connect`` body — where to send the user to authorize."""

    authorization_url: str


class SocialAccountPublic(BaseModel):
    """Public projection of a :class:`app.domain.publishing.social_account.SocialAccount`.

    Non-secret profile only — no credential material is present (C8).
    """

    id: UUID
    user_id: UUID
    platform: str
    external_account_id: str
    display_name: str | None
    status: AccountStatus
    scopes: list[str]
    connected_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "ConnectSocialAccountRequest",
    "ConnectSocialAccountResponse",
    "SocialAccountPublic",
]
