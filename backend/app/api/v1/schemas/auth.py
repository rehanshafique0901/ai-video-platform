"""DTOs for ``/api/v1/auth/*`` endpoints (Slice α2a covers register + login).

The request DTOs lowercase-normalise the email (approved improvement D)
before it ever reaches the use case, so the DB ``CITEXT`` column
receives a canonical value. The response DTOs match API_CONTRACT §3.1
exactly — ``{ user, access_token, refresh_token }`` — wrapped in the
success envelope from §1.1.

``UserPublic`` moved to :mod:`app.api.v1.schemas.users` in α3.3 so that
user-shaped and auth-shaped DTOs live in separate modules. It is still
imported here because :class:`AuthTokensPayload` (the register / login
response body) embeds it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.api.v1.schemas.users import UserPublic

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


class RefreshRequest(BaseModel):
    """POST /api/v1/auth/refresh body.

    Refresh tokens are opaque strings from the client's perspective —
    no length or shape validation beyond a permissive upper bound. The
    ``RefreshSession`` use case owns cryptographic validation.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    refresh_token: str = Field(min_length=1, max_length=4096)


# ---- Responses --------------------------------------------------------


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
