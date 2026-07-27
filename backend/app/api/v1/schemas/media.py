"""DTOs for ``/api/v1/media/*`` endpoints (α6.2).

Register-by-metadata (Q2): the client supplies storage coordinates for an object
it already holds; the API makes no provider/storage call. Mirrors the discipline
in ``schemas/prompts.py``:

* :class:`MediaRegisterRequest` — ``POST /media`` body. ``extra="forbid"`` turns
  any non-declared key (``id``, ``owner_user_id``, ``tenant_id``, ``created_at``,
  …) into a 422 — identity + ownership are server-owned (from ``CurrentUserDep``,
  never the body, Q4). ``source`` is restricted to ``uploaded`` / ``stock``
  (``generated`` is an α8 concern, Q2).
* :class:`MediaPublic` — the response projection. **No** ``version`` field: media
  carries no optimistic-concurrency token (ADR-0037 / Q3). ``tenant_id`` /
  ``owner_user_id`` / ``deleted_at`` are omitted (server-internal / caller-implicit).
  ``checksum_sha256`` is surfaced as a 64-char lowercase hex string.
* :class:`MediaUpdateRequest` — ``PATCH /media/{id}`` body: partial, **narrow**
  (Q8). Only the four links + ``provider`` + ``source_metadata`` are mutable;
  the physical-object columns (``storage_*`` / ``checksum`` / ``mime`` / ``size``
  / dimensions / ``kind`` / ``source``) are immutable — ``extra="forbid"``
  rejects them with a 422. Tri-state resolved by the router via
  ``model_dump(exclude_unset=True)``. Empty patch → 422.

``kind`` / ``source`` / ``storage_backend`` are validated against the physical
enum value sets (``enums.py`` / baseline 0001).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

# Physical ``media_kind`` enum (``enums.py`` / baseline 0001).
MediaKind = Literal[
    "image",
    "video",
    "narration",
    "subtitle",
    "music",
    "sound_effect",
    "thumbnail",
]

# ``media_source`` — full physical enum is {generated, uploaded, stock}. α6.2
# registers already-held objects only, so the REGISTER body accepts the
# non-``generated`` subset (Q2); ``generated`` is minted server-side in α8.
RegisterMediaSource = Literal["uploaded", "stock"]

# Physical ``storage_backend`` enum.
StorageBackend = Literal["local", "s3", "r2", "azure_blob", "gcs"]

_MAX_TEXT = 2_048
_CHECKSUM_HEX_LEN = 64  # sha256 = 32 bytes = 64 hex chars


class MediaRegisterRequest(BaseModel):
    """POST /api/v1/media body (register-by-metadata).

    Physical fields (``kind`` / ``source`` / ``storage_*`` / ``mime_type`` /
    ``size_bytes`` / ``checksum_sha256``) are required; ``project_id`` /
    ``scene_id`` / ``prompt_id`` / ``model_id`` are optional links (validated in
    the use case — a foreign/unknown link → 422); ``provider`` / ``width`` /
    ``height`` / ``duration_seconds`` / ``source_metadata`` are optional
    descriptors. ``checksum_sha256`` is a 64-char hex string.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    kind: MediaKind
    source: RegisterMediaSource
    storage_backend: StorageBackend
    storage_bucket: str = Field(min_length=1, max_length=_MAX_TEXT)
    storage_key: str = Field(min_length=1, max_length=_MAX_TEXT)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(
        min_length=_CHECKSUM_HEX_LEN,
        max_length=_CHECKSUM_HEX_LEN,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    project_id: UUID | None = Field(default=None)
    scene_id: UUID | None = Field(default=None)
    prompt_id: UUID | None = Field(default=None)
    model_id: UUID | None = Field(default=None)
    provider: str | None = Field(default=None, max_length=_MAX_TEXT)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    def checksum_bytes(self) -> bytes:
        """Decode the validated hex ``checksum_sha256`` into raw 32 bytes."""
        return bytes.fromhex(self.checksum_sha256)


class MediaPromoteRequest(BaseModel):
    """POST /api/v1/media/promotions body (α8.8 — Asset Promotion Bridge).

    Promotes a completed AI-generation's final rendered video into the caller's media
    library (the ADR-0046 X8 seam). ``generation_id`` names the execution-plane
    generation to promote; ``project_id`` is the required, ownership-validated link the
    new ``media_assets`` row is attached to (ownership itself comes from
    ``CurrentUserDep``, never the body). ``extra="forbid"`` rejects any other key.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    generation_id: UUID
    project_id: UUID


class MediaPublic(BaseModel):
    """Public projection of :class:`app.domain.media.media_asset.MediaAsset`.

    No ``version`` (media is not concurrency-controlled — ADR-0037);
    ``tenant_id`` / ``owner_user_id`` / ``deleted_at`` are omitted (server-internal
    / caller-implicit). ``checksum_sha256`` is emitted as a 64-char lowercase hex
    string.
    """

    id: UUID
    kind: str
    source: str
    project_id: UUID | None
    scene_id: UUID | None
    prompt_id: UUID | None
    model_id: UUID | None
    provider: str | None
    storage_backend: str
    storage_bucket: str
    storage_key: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_seconds: float | None
    checksum_sha256: bytes
    source_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @field_serializer("checksum_sha256")
    def _serialize_checksum(self, value: bytes) -> str:
        """Emit the raw checksum bytes as a lowercase hex string."""
        return value.hex()


class MediaUpdateRequest(BaseModel):
    """PATCH /api/v1/media/{media_id} body (narrow, Q8).

    Partial update of the **mutable** subset only — the four links
    (``project_id`` / ``scene_id`` / ``prompt_id`` / ``model_id``, all nullable →
    explicit ``null`` clears), ``provider`` (nullable), and ``source_metadata``.
    The physical-object columns are immutable; ``extra="forbid"`` rejects them
    with a 422. **No ``version``** — media has no OCC fence (ADR-0037), so PATCH
    is last-writer-wins. Tri-state (absent = unchanged; explicit ``null`` = clear;
    value = set) is resolved by the router via ``model_dump(exclude_unset=True)``;
    the defaults below are inert placeholders that only make the fields optional.
    Empty patch → 422.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    project_id: UUID | None = Field(default=None)
    scene_id: UUID | None = Field(default=None)
    prompt_id: UUID | None = Field(default=None)
    model_id: UUID | None = Field(default=None)
    provider: str | None = Field(default=None, max_length=_MAX_TEXT)
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_metadata")
    @classmethod
    def _forbid_null_source_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """``source_metadata`` is non-nullable — an explicit ``null`` is a 422.

        Pydantic already rejects ``null`` for a non-``Optional`` field; this
        validator is a belt-and-braces guard documenting the intent.
        """
        return value

    @model_validator(mode="after")
    def _require_mutable_field(self) -> Self:
        """Reject an empty patch (no mutable field supplied)."""
        if not self.model_fields_set:
            raise ValueError(
                "at least one mutable field (project_id, scene_id, prompt_id, "
                "model_id, provider, source_metadata) is required"
            )
        return self
