"""Unit tests for the Media Library API DTOs (Slice α9.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.library import (
    LibraryAssetCreateRequest,
    LibraryAssetPublic,
    LibraryAssetUpdateRequest,
    LibraryAssetUseRequest,
    LibraryFolderCreateRequest,
    LibraryFolderPublic,
    LibraryFolderUpdateRequest,
)
from app.domain.library.library_asset import LibraryAsset
from app.domain.library.library_folder import LibraryFolder

pytestmark = pytest.mark.unit


def test_folder_create_strips_and_requires_name() -> None:
    dto = LibraryFolderCreateRequest(name="  Clips  ")
    assert dto.name == "Clips"
    with pytest.raises(ValidationError):
        LibraryFolderCreateRequest(name="   ")


def test_folder_create_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LibraryFolderCreateRequest(name="X", owner_user_id=uuid4())  # type: ignore[call-arg]


def test_folder_update_requires_a_field() -> None:
    with pytest.raises(ValidationError):
        LibraryFolderUpdateRequest()
    # name-only is fine
    dto = LibraryFolderUpdateRequest(name="New")
    assert dto.model_fields_set == {"name"}
    # explicit null parent clears
    dto2 = LibraryFolderUpdateRequest(parent_folder_id=None)
    assert "parent_folder_id" in dto2.model_fields_set


def test_asset_create_requires_media_and_caps_tags() -> None:
    with pytest.raises(ValidationError):
        LibraryAssetCreateRequest()  # type: ignore[call-arg]
    dto = LibraryAssetCreateRequest(media_asset_id=uuid4(), tags=["a", "b"])
    assert dto.name is None
    with pytest.raises(ValidationError):
        LibraryAssetCreateRequest(media_asset_id=uuid4(), tags=[f"t{i}" for i in range(51)])
    with pytest.raises(ValidationError):
        LibraryAssetCreateRequest(media_asset_id=uuid4(), tags=["x" * 65])


def test_asset_update_requires_version_and_a_mutable_field() -> None:
    with pytest.raises(ValidationError):
        LibraryAssetUpdateRequest()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        LibraryAssetUpdateRequest(version=1)  # only version → empty patch
    dto = LibraryAssetUpdateRequest(version=2, name="R")
    assert dto.version == 2
    assert dto.model_fields_set == {"version", "name"}


def test_asset_use_request_requires_project_id() -> None:
    with pytest.raises(ValidationError):
        LibraryAssetUseRequest()  # type: ignore[call-arg]
    dto = LibraryAssetUseRequest(project_id=uuid4())
    assert dto.project_id is not None


def test_public_projections_round_trip() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    folder = LibraryFolder(
        id=uuid4(),
        tenant_id=uuid4(),
        owner_user_id=uuid4(),
        parent_folder_id=None,
        name="F",
        created_at=now,
        updated_at=now,
    )
    fp = LibraryFolderPublic.from_domain(folder)
    assert fp.name == "F"

    asset = LibraryAsset(
        id=uuid4(),
        tenant_id=uuid4(),
        owner_user_id=uuid4(),
        media_asset_id=uuid4(),
        library_folder_id=None,
        name="A",
        description=None,
        tags=("x", "y"),
        usage_count=3,
        last_used_at=now,
        version=2,
        created_at=now,
        updated_at=now,
    )
    ap = LibraryAssetPublic.from_domain(asset)
    assert ap.tags == ["x", "y"]
    assert ap.usage_count == 3
    assert ap.version == 2
