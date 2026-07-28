"""DTOs for ``/api/v1/library/*`` endpoints (Slice α9.2 — Media Library).

Folders and library assets over registered ``media_assets`` (ADR-0037 CR-8). The
field whitelist is enforced by ``extra="forbid"`` (ownership/tenancy/identity come from
the authenticated caller, never the body). Update DTOs use tri-state semantics resolved
by the router via ``model_fields_set`` (mirrors ``schemas/projects.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.library.library_asset import LibraryAsset
from app.domain.library.library_folder import LibraryFolder

_MAX_TAGS = 50
_MAX_TAG_LEN = 64


def _validate_tags(value: list[str]) -> list[str]:
    if len(value) > _MAX_TAGS:
        raise ValueError(f"at most {_MAX_TAGS} tags are allowed")
    for tag in value:
        if len(tag) > _MAX_TAG_LEN:
            raise ValueError(f"each tag must be at most {_MAX_TAG_LEN} characters")
    return value


# --- folders ----------------------------------------------------------------


class LibraryFolderCreateRequest(BaseModel):
    """POST /api/v1/library/folders body."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    parent_folder_id: UUID | None = Field(default=None)


class LibraryFolderUpdateRequest(BaseModel):
    """PATCH /api/v1/library/folders/{id} body — partial (last-writer-wins)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(default="", min_length=1, max_length=200)
    parent_folder_id: UUID | None = Field(default=None)

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        if not set(self.model_fields_set):
            raise ValueError("at least one mutable field (name, parent_folder_id) is required")
        return self


class LibraryFolderPublic(BaseModel):
    """Public projection of :class:`app.domain.library.library_folder.LibraryFolder`."""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    parent_folder_id: UUID | None
    name: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, folder: LibraryFolder) -> LibraryFolderPublic:
        return cls(
            id=folder.id,
            tenant_id=folder.tenant_id,
            owner_user_id=folder.owner_user_id,
            parent_folder_id=folder.parent_folder_id,
            name=folder.name,
            created_at=folder.created_at,
            updated_at=folder.updated_at,
        )


# --- assets -----------------------------------------------------------------


class LibraryAssetCreateRequest(BaseModel):
    """POST /api/v1/library/assets body."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    media_asset_id: UUID
    library_folder_id: UUID | None = Field(default=None)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list)

    _check_tags = field_validator("tags")(_validate_tags)


class LibraryAssetUpdateRequest(BaseModel):
    """PATCH /api/v1/library/assets/{id} body — partial, version-fenced update."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(
        ge=1,
        description=(
            "The ``version`` the client last observed on the target library asset. "
            "A stale value yields 412 VERSION_CONFLICT."
        ),
    )
    name: str = Field(default="", min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list)
    library_folder_id: UUID | None = Field(default=None)

    _check_tags = field_validator("tags")(_validate_tags)

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        if not (set(self.model_fields_set) - {"version"}):
            raise ValueError(
                "at least one mutable field "
                "(name, description, tags, library_folder_id) is required"
            )
        return self


class LibraryAssetUseRequest(BaseModel):
    """POST /api/v1/library/assets/{id}/uses body."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID


class LibraryAssetPublic(BaseModel):
    """Public projection of :class:`app.domain.library.library_asset.LibraryAsset`."""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    media_asset_id: UUID
    library_folder_id: UUID | None
    name: str
    description: str | None
    tags: list[str]
    usage_count: int
    last_used_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, asset: LibraryAsset) -> LibraryAssetPublic:
        return cls(
            id=asset.id,
            tenant_id=asset.tenant_id,
            owner_user_id=asset.owner_user_id,
            media_asset_id=asset.media_asset_id,
            library_folder_id=asset.library_folder_id,
            name=asset.name,
            description=asset.description,
            tags=list(asset.tags),
            usage_count=asset.usage_count,
            last_used_at=asset.last_used_at,
            version=asset.version,
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )
