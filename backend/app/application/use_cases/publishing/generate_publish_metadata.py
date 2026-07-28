"""``GeneratePublishMetadata`` use case (α9.1, ADR-0049).

Suggests publish metadata (title / description / hashtags) for a creator's finished, owned export.
Opt-in and **advisory only**: it is entirely separate from ``CreatePublishJob`` and never a
prerequisite for creating a publish job. The final metadata is whatever the creator ultimately
submits to ``POST /publish-jobs`` (user edits always win); nothing is persisted here.

Order (α9.1 requirement — cheap validation before expensive AI work):

1. **ownership** — resolve the export → owning project, owner-scoped (``404`` otherwise);
2. **export readiness / publish eligibility** — the export must be ``succeeded`` with a delivery
   artifact (``422`` otherwise) — the same gate ``CreatePublishJob`` applies;
3. only then **invoke AI** through the Publishing-owned :class:`IPublishMetadataGenerator` port,
   with a **mandatory deterministic fallback** to the existing template on any AI failure
   (ADR-0049 Invariants 1–3). The AI call runs *after* the read UoW is closed — a slow provider
   never holds a DB transaction open.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.application.interfaces.publish_metadata_generator import (
    GeneratedPublishMetadata,
    IPublishMetadataGenerator,
    MetadataProvenance,
    PublishMetadataGenerationError,
    PublishMetadataRequest,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.export.export_status import ExportStatus
from app.domain.projects.project import Project
from app.domain.publishing.content_package import build_content_package

_LOGGER = structlog.get_logger(__name__)

# Strictest known destination caps (YouTube — the strictest today): a suggestion that fits these
# can never become a permanent ``invalid_metadata`` publish failure. The destination adapter still
# enforces its own limits at the boundary (defence in depth).
_MAX_TITLE_CHARS = 100
_MAX_DESCRIPTION_CHARS = 5000
_MAX_TAGS_TOTAL_CHARS = 500
_MAX_TAG_COUNT = 15


def _build_context(project: Project | None) -> str:
    """Assemble a plain, single-line video description from the owned project (no LLM text here)."""
    if project is None:
        return ""
    parts = [project.name.strip()]
    if project.description:
        parts.append(project.description.strip())
    return ". ".join(p for p in parts if p)


class GeneratePublishMetadata:
    """Owner-scoped, advisory publish-metadata suggestion with a deterministic fallback."""

    def __init__(self, uow: IUnitOfWork, generator: IPublishMetadataGenerator) -> None:
        self._uow = uow
        self._generator = generator

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        export_job_id: UUID,
        request_id: str,
    ) -> GeneratedPublishMetadata:
        # --- Cheap validation first (ownership → readiness / eligibility) ---------------
        async with self._uow:
            source = await self._uow.publish_jobs.resolve_source(
                export_job_id, tenant_id=tenant_id, owner_user_id=owner_user_id
            )
            if source is None:
                raise NotFoundError(
                    "export job not found", details={"export_job_id": str(export_job_id)}
                )
            if (
                source.export_status != ExportStatus.SUCCEEDED.value
                or source.source_media_asset_id is None
            ):
                raise ValidationFailedError(
                    "export job has no completed delivery artifact to publish",
                    details={
                        "export_job_id": str(export_job_id),
                        "status": source.export_status,
                    },
                )
            source_media_asset_id = source.source_media_asset_id
            project = await self._uow.projects.get_owned(
                project_id=source.project_id, tenant_id=tenant_id, owner_user_id=owner_user_id
            )

        # --- Expensive AI work, only after validation, outside the DB transaction -------
        project_title = project.name if project is not None else None
        request = PublishMetadataRequest(
            request_id=request_id,
            context=_build_context(project),
            max_title_chars=_MAX_TITLE_CHARS,
            max_description_chars=_MAX_DESCRIPTION_CHARS,
            max_tags_total_chars=_MAX_TAGS_TOTAL_CHARS,
            max_tag_count=_MAX_TAG_COUNT,
        )
        try:
            suggestion = await self._generator.generate(request)
        except PublishMetadataGenerationError:
            _LOGGER.info(
                "publish_metadata.fallback",
                export_job_id=str(export_job_id),
                owner_user_id=str(owner_user_id),
            )
            return self._deterministic_fallback(source_media_asset_id, project_title)

        _LOGGER.info(
            "publish_metadata.suggested",
            export_job_id=str(export_job_id),
            owner_user_id=str(owner_user_id),
            generator=suggestion.provenance.generator,
        )
        return suggestion

    @staticmethod
    def _deterministic_fallback(
        source_media_asset_id: UUID, project_title: str | None
    ) -> GeneratedPublishMetadata:
        """The existing deterministic template (PUB-9) — the always-available primary path."""
        package = build_content_package(
            media_asset_id=source_media_asset_id, project_title=project_title
        )
        return GeneratedPublishMetadata(
            title=package.title,
            description=package.description,
            tags=package.tags,
            provenance=MetadataProvenance(generator="template", is_fallback=True),
        )
