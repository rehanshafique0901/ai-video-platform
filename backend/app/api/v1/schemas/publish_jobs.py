"""DTOs for ``/api/v1/publish-jobs/*`` (α8.6b Publish Runtime).

The wire contract for the creator publish flow. The request names the finished export to
publish + the destination account (PUB-1/PUB-2) plus optional deterministic metadata
overrides (PUB-9). The response projects the publish job + its content package — **no**
credential, bearer, or platform-internal field is ever present (PUB-8 / ADR-0047 C8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.domain.publishing.content_package import ContentPackage, Visibility


class PublishJobCreateRequest(BaseModel):
    """``POST /publish-jobs`` body — what to publish + where + optional metadata.

    ``publish_at`` (α8.9b — Creator Scheduling) is an optional platform-native schedule: when
    set, the job still uploads immediately, but the destination keeps the video private and
    flips it live at ``publish_at`` (YouTube ``status.publishAt``). It is **not** worker-side
    deferral — the runtime is unchanged. Absent ⇒ today's immediate-publish behaviour.
    """

    export_job_id: UUID
    social_account_id: UUID
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=50)
    visibility: Visibility | None = None
    publish_at: datetime | None = None
    # α9.3 — optional creator-supplied thumbnail (ADR-0050 Option A). Owner + image-kind are
    # verified in CreatePublishJob (they need a DB read → 404/422); no boundary validator here.
    thumbnail_media_asset_id: UUID | None = None

    @field_validator("publish_at")
    @classmethod
    def _validate_publish_at(cls, value: datetime | None) -> datetime | None:
        """Timezone-aware + strictly future; normalised to UTC (SC3).

        A naive datetime or a non-future time is a 422 (a raised ``ValueError`` in a Pydantic
        validator surfaces through the app's exception handlers). Normalising to UTC keeps the
        stored/echoed ``ContentPackage.publish_at`` canonical and deterministic.
        """
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publish_at must be timezone-aware (include a UTC offset)")
        if value <= datetime.now(UTC):
            raise ValueError("publish_at must be in the future")
        return value.astimezone(UTC)


class ContentPackagePublic(BaseModel):
    """Public projection of the deterministic :class:`ContentPackage` (PUB-9)."""

    media_asset_id: UUID
    title: str
    description: str
    tags: list[str]
    visibility: Visibility
    thumbnail_media_asset_id: UUID | None
    publish_at: datetime | None

    @classmethod
    def from_domain(cls, package: ContentPackage) -> ContentPackagePublic:
        return cls(
            media_asset_id=package.media_asset_id,
            title=package.title,
            description=package.description,
            tags=list(package.tags),
            visibility=package.visibility,
            thumbnail_media_asset_id=package.thumbnail_media_asset_id,
            publish_at=package.publish_at,
        )


class PublishJobPublic(BaseModel):
    """Public projection of a :class:`app.domain.publishing.publish_job.PublishJob`.

    No credential material (C8). ``error`` is the neutral ``code``/``message`` dict recorded
    on failure — never a bearer or platform internal.
    """

    id: UUID
    requested_by_user_id: UUID
    project_id: UUID
    source_export_job_id: UUID
    source_media_asset_id: UUID
    social_account_id: UUID
    platform: str
    status: str
    scheduled_at: datetime | None
    attempt: int
    max_attempts: int
    content_package: ContentPackagePublic
    platform_post_id: str | None
    platform_post_url: str | None
    error: dict[str, Any] | None
    published_at: datetime | None
    finished_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


__all__ = [
    "PublishJobCreateRequest",
    "ContentPackagePublic",
    "PublishJobPublic",
]
