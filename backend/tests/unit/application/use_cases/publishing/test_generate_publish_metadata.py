"""Unit tests for ``GeneratePublishMetadata`` (in-memory fakes, no DB).

Prove the α9.1 orchestration + boundaries (ADR-0049): cheap validation first (ownership → export
readiness / eligibility), provider independence (the use case only ever touches the port), the
mandatory deterministic fallback on any AI failure, and that user-facing errors (404/422) are
raised *before* any AI work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.application.interfaces.publish_metadata_generator import (
    GeneratedPublishMetadata,
    IPublishMetadataGenerator,
    MetadataProvenance,
    PublishMetadataGenerationError,
    PublishMetadataRequest,
)
from app.application.use_cases.publishing.generate_publish_metadata import GeneratePublishMetadata
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.projects.project import Project
from app.domain.publishing.publish_job import PublishSource

pytestmark = pytest.mark.unit

_TENANT = uuid4()
_USER = uuid4()
_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _project(
    project_id: UUID, *, name: str = "My Project", description: str | None = None
) -> Project:
    return Project(
        id=project_id,
        tenant_id=_TENANT,
        owner_user_id=_USER,
        folder_id=None,
        current_version_id=None,
        name=name,
        description=description,
        aspect_ratio="16:9",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=_NOW,
        updated_at=_NOW,
        version=1,
    )


def _source(project_id: UUID, media_id: UUID | None, *, status: str = "succeeded") -> PublishSource:
    return PublishSource(
        export_job_id=uuid4(),
        project_id=project_id,
        source_media_asset_id=media_id,
        export_status=status,
    )


class _FakePublishJobs:
    def __init__(self, source: PublishSource | None) -> None:
        self._source = source
        self.resolve_calls = 0

    async def resolve_source(
        self, export_job_id, *, tenant_id, owner_user_id
    ) -> PublishSource | None:
        self.resolve_calls += 1
        return self._source


class _FakeProjects:
    def __init__(self, project: Project | None) -> None:
        self._project = project

    async def get_owned(self, *, project_id, tenant_id, owner_user_id) -> Project | None:
        return self._project


class _FakeUoW:
    def __init__(self, *, source: PublishSource | None, project: Project | None) -> None:
        self.publish_jobs = _FakePublishJobs(source)
        self.projects = _FakeProjects(project)
        self.entered = False

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _RecordingGenerator(IPublishMetadataGenerator):
    """Deterministic stub that records the request it received."""

    def __init__(self) -> None:
        self.requests: list[PublishMetadataRequest] = []

    async def generate(self, req: PublishMetadataRequest) -> GeneratedPublishMetadata:
        self.requests.append(req)
        return GeneratedPublishMetadata(
            title=f"AI: {req.context}"[: req.max_title_chars],
            description=req.context,
            tags=("alpha", "beta"),
            provenance=MetadataProvenance(
                generator="llm", is_fallback=False, model="stub", prompt_template_version="v1"
            ),
        )


class _FailingGenerator(IPublishMetadataGenerator):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, req: PublishMetadataRequest) -> GeneratedPublishMetadata:
        self.calls += 1
        raise PublishMetadataGenerationError("boom")


class _ExplodingGenerator(IPublishMetadataGenerator):
    """Must never be called — proves validation runs before any AI work."""

    async def generate(self, req: PublishMetadataRequest) -> GeneratedPublishMetadata:
        raise AssertionError("AI must not be invoked before validation passes")


async def _run(uow: _FakeUoW, generator: IPublishMetadataGenerator) -> GeneratedPublishMetadata:
    use_case = GeneratePublishMetadata(uow=uow, generator=generator)  # type: ignore[arg-type]
    return await use_case.execute(
        tenant_id=_TENANT,
        owner_user_id=_USER,
        export_job_id=uuid4(),
        request_id="req-1",
    )


async def test_happy_path_returns_llm_suggestion_within_caps() -> None:
    project_id = uuid4()
    media_id = uuid4()
    uow = _FakeUoW(source=_source(project_id, media_id), project=_project(project_id))
    generator = _RecordingGenerator()

    result = await _run(uow, generator)

    assert result.provenance.generator == "llm"
    assert result.provenance.is_fallback is False
    assert result.tags == ("alpha", "beta")
    # Provider independence: the use case handed the port a neutral request built from the project.
    (req,) = generator.requests
    assert isinstance(req, PublishMetadataRequest)
    assert req.context == "My Project"
    assert req.max_title_chars == 100 and req.max_tag_count == 15


async def test_context_includes_project_description() -> None:
    project_id = uuid4()
    uow = _FakeUoW(
        source=_source(project_id, uuid4()),
        project=_project(project_id, name="Trip", description="A road trip vlog"),
    )
    generator = _RecordingGenerator()
    await _run(uow, generator)
    (req,) = generator.requests
    assert req.context == "Trip. A road trip vlog"


async def test_unknown_export_is_404_before_ai() -> None:
    uow = _FakeUoW(source=None, project=None)
    with pytest.raises(NotFoundError):
        await _run(uow, _ExplodingGenerator())


async def test_export_not_succeeded_is_422_before_ai() -> None:
    project_id = uuid4()
    uow = _FakeUoW(
        source=_source(project_id, uuid4(), status="running"), project=_project(project_id)
    )
    with pytest.raises(ValidationFailedError):
        await _run(uow, _ExplodingGenerator())


async def test_export_without_delivery_is_422_before_ai() -> None:
    project_id = uuid4()
    uow = _FakeUoW(source=_source(project_id, None), project=_project(project_id))
    with pytest.raises(ValidationFailedError):
        await _run(uow, _ExplodingGenerator())


async def test_ai_failure_falls_back_to_deterministic_template() -> None:
    project_id = uuid4()
    media_id = uuid4()
    uow = _FakeUoW(
        source=_source(project_id, media_id),
        project=_project(project_id, name="Fallback Project"),
    )
    generator = _FailingGenerator()

    result = await _run(uow, generator)

    assert generator.calls == 1
    assert result.provenance.generator == "template"
    assert result.provenance.is_fallback is True
    # The template reuses build_content_package: title from the project, empty tags.
    assert result.title == "Fallback Project"
    assert result.description == "Fallback Project"
    assert result.tags == ()


async def test_fallback_title_defaults_when_project_missing() -> None:
    project_id = uuid4()
    uow = _FakeUoW(source=_source(project_id, uuid4()), project=None)
    result = await _run(uow, _FailingGenerator())
    assert result.provenance.is_fallback is True
    assert result.title == "Untitled video"
