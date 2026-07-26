"""``ContentPackage`` — the platform-agnostic description of *what to publish* (α8.6b).

Immutable, deterministic (PUB-9), and **platform-neutral**: a destination adapter maps it
to a platform-specific request at the boundary and validates platform limits there
(contract §5). In α8.6b the metadata is a **pure template** of the source artifact + project
context — no LLM, no randomness (the ``ContentPackage → LLM Metadata Generator`` is a later
slice).

Serialised to/from the ``publish_jobs.content_package`` JSONB column by the repository via
:meth:`ContentPackage.to_dict` / :meth:`ContentPackage.from_dict`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class Visibility(StrEnum):
    """Requested post visibility. Default is :data:`PRIVATE` (safe default, ruled 2026-07-27)."""

    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class ContentPackage:
    """Immutable, platform-agnostic publish payload (deterministic in α8.6b)."""

    media_asset_id: UUID
    title: str
    description: str
    tags: tuple[str, ...]
    visibility: Visibility
    thumbnail_media_asset_id: UUID | None
    publish_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        """JSONB-serialisable projection (all values are JSON-native)."""
        return {
            "media_asset_id": str(self.media_asset_id),
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "visibility": self.visibility.value,
            "thumbnail_media_asset_id": (
                str(self.thumbnail_media_asset_id)
                if self.thumbnail_media_asset_id is not None
                else None
            ),
            "publish_at": self.publish_at.isoformat() if self.publish_at is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ContentPackage:
        """Reconstruct from the stored ``content_package`` JSONB."""
        thumb = raw.get("thumbnail_media_asset_id")
        publish_at = raw.get("publish_at")
        return cls(
            media_asset_id=UUID(str(raw["media_asset_id"])),
            title=str(raw["title"]),
            description=str(raw["description"]),
            tags=tuple(str(t) for t in raw.get("tags", [])),
            visibility=Visibility(str(raw["visibility"])),
            thumbnail_media_asset_id=UUID(str(thumb)) if thumb is not None else None,
            publish_at=datetime.fromisoformat(str(publish_at)) if publish_at is not None else None,
        )


_DEFAULT_TITLE = "Untitled video"


def build_content_package(
    *,
    media_asset_id: UUID,
    project_title: str | None,
    thumbnail_media_asset_id: UUID | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] | None = None,
    visibility: Visibility | None = None,
    publish_at: datetime | None = None,
) -> ContentPackage:
    """Build a deterministic :class:`ContentPackage` (PUB-9).

    A pure function of the source artifact + project context + optional user overrides.
    Absent overrides fall back to fixed templates (title from the project, an empty tag set,
    :data:`Visibility.PRIVATE`) — never an LLM or any nondeterministic source.
    """
    resolved_title = (title or project_title or _DEFAULT_TITLE).strip() or _DEFAULT_TITLE
    resolved_description = description if description is not None else resolved_title
    return ContentPackage(
        media_asset_id=media_asset_id,
        title=resolved_title,
        description=resolved_description,
        tags=tags if tags is not None else (),
        visibility=visibility if visibility is not None else Visibility.PRIVATE,
        thumbnail_media_asset_id=thumbnail_media_asset_id,
        publish_at=publish_at,
    )


__all__ = ["ContentPackage", "Visibility", "build_content_package"]
