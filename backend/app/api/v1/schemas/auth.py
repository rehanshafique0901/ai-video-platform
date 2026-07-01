"""DTOs for ``/api/v1/auth/*`` endpoints (Slice α2a covers register + login).

The request DTOs lowercase-normalise the email (approved improvement D)
before it ever reaches the use case, so the DB ``CITEXT`` column
receives a canonical value. The response DTOs match API_CONTRACT §3.1
exactly — ``{ user, access_token, refresh_token }`` — wrapped in the
success envelope from §1.1.

``UserPublic`` intentionally omits ``password_hash`` and any internal
audit column. Pydantic v2 with explicit fields (not ``model_config``
``exclude``) gives compile-time safety: adding a new sensitive field to
the domain ``User`` entity does *not* leak into the response unless the
DTO is edited to include it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# ---- Requests ---------------------------------------------------------


class RegisterRequest(BaseModel):
    """POST /api/v1/auth/register body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    password: str = Field(
        min_length=8,
        max_length=128,
        description="Minimum 8 chars (OWASP ASVS §2.1.1); no complexity policy in α2a.",
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        description="Display name shown in UI; not required to be unique.",
    )

    @field_validator("email", mode="after")
    @classmethod
    def _lowercase(cls, v: str) -> str:
        return v.lower()


class LoginRequest(BaseModel):
    """POST /api/v1/auth/login body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    # Wider bound than register — accept whatever the account already
    # holds. Never lower-bound to a specific complexity here.
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email", mode="after")
    @classmethod
    def _lowercase(cls, v: str) -> str:
        return v.lower()


# ---- Responses --------------------------------------------------------


class UserPublic(BaseModel):
    """Public projection of ``User``. ``password_hash`` is intentionally absent."""

    id: UUID
    tenant_id: UUID
    email: str
    display_name: str
    email_verified_at: datetime | None
    created_at: datetime


class AuthTokensPayload(BaseModel):
    """Inner payload of the register / login responses."""

    user: UserPublic
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"


class AuthTokensEnvelope(BaseModel):
    """API_CONTRACT §1.1 success envelope wrapping :class:`AuthTokensPayload`."""

    data: AuthTokensPayload
    meta: dict[str, Any]
