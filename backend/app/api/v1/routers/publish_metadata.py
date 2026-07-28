"""``/api/v1/publish-metadata/*`` router (α9.1 — AI Caption & Hashtag Generation).

A single authenticated, opt-in endpoint that *suggests* publish metadata (title / description /
hashtags) for a creator's finished, owned export. Advisory only (ADR-0049): it never creates a
publish job and is never a prerequisite for one — the creator takes, edits, or discards the
suggestion and submits final metadata to ``POST /publish-jobs`` separately.

* ``POST /publish-metadata/suggestions`` → 200, a suggestion (``provenance.generator`` = ``llm``)
  or the deterministic fallback (``provenance.is_fallback`` = ``true``) — an AI failure never
  fails the request. ``404`` for an unknown/foreign export; ``422`` for a not-ready export or a
  malformed body; ``401`` if unauthenticated.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.deps import CurrentUserDep, GeneratePublishMetadataDep
from app.api.v1.helpers import envelope
from app.api.v1.schemas.publish_metadata import (
    PublishMetadataSuggestionPublic,
    PublishMetadataSuggestRequest,
)

router = APIRouter(prefix="/publish-metadata", tags=["publish-metadata"])


@router.post("/suggestions")
async def suggest_publish_metadata(
    body: PublishMetadataSuggestRequest,
    request: Request,
    current_user: CurrentUserDep,
    use_case: GeneratePublishMetadataDep,
) -> JSONResponse:
    """Suggest advisory publish metadata for the caller's finished, owned export."""
    suggestion = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        export_job_id=body.export_job_id,
        request_id=getattr(request.state, "request_id", ""),
    )
    return JSONResponse(
        content=envelope(PublishMetadataSuggestionPublic.from_domain(suggestion), request)
    )
