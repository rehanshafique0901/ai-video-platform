"""Unit tests for the deterministic ``ContentPackage`` + builder (α8.6b, PUB-9)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.publishing.content_package import (
    ContentPackage,
    Visibility,
    build_content_package,
)


@pytest.mark.unit
def test_builder_defaults_are_deterministic_and_private() -> None:
    media_id = uuid4()
    a = build_content_package(media_asset_id=media_id, project_title="My Project")
    b = build_content_package(media_asset_id=media_id, project_title="My Project")
    assert a == b
    assert a.title == "My Project"
    assert a.description == "My Project"
    assert a.tags == ()
    assert a.visibility is Visibility.PRIVATE
    assert a.thumbnail_media_asset_id is None
    assert a.publish_at is None


@pytest.mark.unit
def test_builder_falls_back_to_untitled_when_no_title_source() -> None:
    pkg = build_content_package(media_asset_id=uuid4(), project_title=None)
    assert pkg.title == "Untitled video"
    pkg_blank = build_content_package(media_asset_id=uuid4(), project_title="   ")
    assert pkg_blank.title == "Untitled video"


@pytest.mark.unit
def test_builder_honours_overrides() -> None:
    pkg = build_content_package(
        media_asset_id=uuid4(),
        project_title="ignored",
        title="Custom Title",
        description="Custom description",
        tags=("a", "b"),
        visibility=Visibility.PUBLIC,
    )
    assert pkg.title == "Custom Title"
    assert pkg.description == "Custom description"
    assert pkg.tags == ("a", "b")
    assert pkg.visibility is Visibility.PUBLIC


@pytest.mark.unit
def test_to_dict_from_dict_round_trip() -> None:
    original = ContentPackage(
        media_asset_id=uuid4(),
        title="Title",
        description="Desc",
        tags=("x", "y"),
        visibility=Visibility.UNLISTED,
        thumbnail_media_asset_id=uuid4(),
        publish_at=datetime(2026, 7, 27, 10, 30, tzinfo=UTC),
    )
    restored = ContentPackage.from_dict(original.to_dict())
    assert restored == original


@pytest.mark.unit
def test_to_dict_is_json_native() -> None:
    pkg = build_content_package(media_asset_id=uuid4(), project_title="P", tags=("t",))
    raw = pkg.to_dict()
    assert isinstance(raw["media_asset_id"], str)
    assert isinstance(raw["tags"], list)
    assert raw["visibility"] == "private"
    assert raw["thumbnail_media_asset_id"] is None
    assert raw["publish_at"] is None
