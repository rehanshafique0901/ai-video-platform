"""Unit tests for the α9.1 publish-metadata API DTOs.

Prove the request DTO rejects a malformed / extra-field body (so the endpoint returns 422 before
any use-case work) and the response projection preserves the explicit provenance contract.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.publish_metadata import (
    PublishMetadataSuggestionPublic,
    PublishMetadataSuggestRequest,
)
from app.application.interfaces.publish_metadata_generator import (
    GeneratedPublishMetadata,
    MetadataProvenance,
)

pytestmark = pytest.mark.unit


def test_request_accepts_valid_uuid() -> None:
    export_id = uuid4()
    req = PublishMetadataSuggestRequest(export_job_id=export_id)
    assert req.export_job_id == export_id


def test_request_rejects_missing_export_job_id() -> None:
    with pytest.raises(ValidationError):
        PublishMetadataSuggestRequest()  # type: ignore[call-arg]


def test_request_rejects_non_uuid() -> None:
    with pytest.raises(ValidationError):
        PublishMetadataSuggestRequest(export_job_id="not-a-uuid")  # type: ignore[arg-type]


def test_request_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PublishMetadataSuggestRequest.model_validate(
            {"export_job_id": str(uuid4()), "title": "sneaky override"}
        )


def test_response_projection_preserves_provenance() -> None:
    meta = GeneratedPublishMetadata(
        title="T",
        description="D",
        tags=("a", "b"),
        provenance=MetadataProvenance(
            generator="llm", is_fallback=False, model="m", prompt_template_version="v1"
        ),
    )
    public = PublishMetadataSuggestionPublic.from_domain(meta)
    dumped = public.model_dump()
    assert dumped["tags"] == ["a", "b"]
    assert dumped["provenance"] == {
        "generator": "llm",
        "is_fallback": False,
        "model": "m",
        "prompt_template_version": "v1",
    }


def test_response_projection_marks_fallback() -> None:
    meta = GeneratedPublishMetadata(
        title="Untitled video",
        description="Untitled video",
        tags=(),
        provenance=MetadataProvenance(generator="template", is_fallback=True),
    )
    public = PublishMetadataSuggestionPublic.from_domain(meta)
    assert public.provenance.generator == "template"
    assert public.provenance.is_fallback is True
    assert public.tags == []
