"""API DTOs for AI publish-metadata suggestions (α9.1, ADR-0049).

The response contract is **explicit** (no implicit behaviour): ``provenance.generator`` is
``"llm"`` for an AI suggestion or ``"template"`` for the deterministic fallback, and
``provenance.is_fallback`` states plainly whether the deterministic path was used. This lets a
client distinguish a **generated suggestion** from a **deterministic fallback**; the third case —
**user-supplied metadata** — is never produced here: whatever the creator ultimately edits and
submits to ``POST /publish-jobs`` always wins (ADR-0049 Invariant 4).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.application.interfaces.publish_metadata_generator import GeneratedPublishMetadata


class PublishMetadataSuggestRequest(BaseModel):
    """Request one advisory metadata suggestion for a finished, owned export."""

    model_config = ConfigDict(extra="forbid")

    export_job_id: UUID


class MetadataProvenancePublic(BaseModel):
    """How the returned suggestion was produced (ephemeral — never persisted)."""

    generator: str
    is_fallback: bool
    model: str | None = None
    prompt_template_version: str | None = None


class PublishMetadataSuggestionPublic(BaseModel):
    """A suggested (or deterministically-fallen-back) set of publish metadata."""

    title: str
    description: str
    tags: list[str]
    provenance: MetadataProvenancePublic

    @classmethod
    def from_domain(cls, meta: GeneratedPublishMetadata) -> PublishMetadataSuggestionPublic:
        return cls(
            title=meta.title,
            description=meta.description,
            tags=list(meta.tags),
            provenance=MetadataProvenancePublic(
                generator=meta.provenance.generator,
                is_fallback=meta.provenance.is_fallback,
                model=meta.provenance.model,
                prompt_template_version=meta.provenance.prompt_template_version,
            ),
        )
