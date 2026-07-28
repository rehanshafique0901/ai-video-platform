"""α9.1 — AI publish-metadata suggestion end-to-end against live PostgreSQL (ADR-0049).

Proves the suggestion pipeline works across the real transaction model: the
:class:`GeneratePublishMetadata` use case reads a committed, owner-scoped, ready export through a
real :class:`SqlAlchemyUnitOfWork`, then produces a deterministic suggestion via the real
:class:`LlmPublishMetadataGenerator` over the default (mock) LLM capability — and degrades to the
deterministic template on any AI failure. A final flow proves the suggestion is reachable through
the real ``POST /api/v1/publish-metadata/suggestions`` endpoint for the authenticated owner.

The use-case tests commit their seed (the use case opens its own UoW/connection, like the other
runtime slices) and delete the rows they created on teardown, leaving the destructive-migration
guard untouched. HTTP-contract cases use the SAVEPOINT-rolled-back ``client`` fixture and need no
seed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.publish_metadata_generator import (
    GeneratedPublishMetadata,
    IPublishMetadataGenerator,
    PublishMetadataGenerationError,
    PublishMetadataRequest,
)
from app.application.use_cases.publishing.generate_publish_metadata import GeneratePublishMetadata
from app.core import container
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.export.export_status import ExportStatus
from app.domain.render.render_status import RenderStatus
from app.infrastructure.ai.metadata.llm_publish_metadata_generator import (
    LlmPublishMetadataGenerator,
)
from app.infrastructure.ai.providers.registry import default_registry
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.jobs import ExportJob as ExportJobRow, RenderJob as RenderJobRow
from app.infrastructure.db.models.media import MediaAsset as MediaAssetRow
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.timeline import Timeline as TimelineRow
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Seed:
    tenant_id: UUID
    user_id: UUID
    project_id: UUID
    timeline_id: UUID
    master_id: UUID
    delivery_id: UUID
    render_id: UUID
    export_id: UUID


async def _insert_seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    user_id: UUID,
    seed_identity: bool,
    export_status: str = ExportStatus.SUCCEEDED.value,
    project_name: str = "My Live Project",
) -> _Seed:
    project_id = uuid4()
    timeline_id = uuid4()
    master_id = uuid4()
    delivery_id = uuid4()
    render_id = uuid4()
    export_id = uuid4()
    async with session_factory() as s:
        if seed_identity:
            await s.execute(
                insert(Tenant).values(id=tenant_id, name="AIM", slug=f"aim-{tenant_id}")
            )
            await s.execute(
                insert(User).values(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=f"aim-{user_id}@example.com",
                    display_name="AIM Owner",
                )
            )
        await s.execute(
            insert(ProjectRow).values(
                id=project_id,
                tenant_id=tenant_id,
                owner_user_id=user_id,
                name=project_name,
                aspect_ratio="horizontal",
            )
        )
        await s.execute(
            insert(TimelineRow).values(
                id=timeline_id,
                project_id=project_id,
                aspect_ratio="16:9",
                frame_rate=30,
                background_color="#000000",
            )
        )
        for mid, key in ((master_id, "master"), (delivery_id, "delivery")):
            await s.execute(
                insert(MediaAssetRow).values(
                    id=mid,
                    tenant_id=tenant_id,
                    owner_user_id=user_id,
                    kind="video",
                    storage_backend="local",
                    storage_bucket="media",
                    storage_key=f"{key}/{mid}.mp4",
                    mime_type="video/mp4",
                    size_bytes=2048,
                    checksum_sha256=b"\x00" * 32,
                    source="generated",
                )
            )
        await s.execute(
            insert(RenderJobRow).values(
                id=render_id,
                project_id=project_id,
                timeline_id=timeline_id,
                pipeline="ffmpeg",
                pipeline_version="0.0.0",
                queue="normal",
                status=RenderStatus.SUCCEEDED.value,
                output_media_asset_id=master_id,
            )
        )
        await s.execute(
            insert(ExportJobRow).values(
                id=export_id,
                render_job_id=render_id,
                requested_by_user_id=user_id,
                format="mp4",
                quality="hd_1080p",
                orientation="horizontal",
                status=export_status,
                output_media_asset_id=delivery_id,
            )
        )
        await s.commit()
    return _Seed(
        tenant_id=tenant_id,
        user_id=user_id,
        project_id=project_id,
        timeline_id=timeline_id,
        master_id=master_id,
        delivery_id=delivery_id,
        render_id=render_id,
        export_id=export_id,
    )


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession], seed: _Seed, *, drop_identity: bool
) -> None:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM export_jobs WHERE id = :i"), {"i": str(seed.export_id)})
        await s.execute(text("DELETE FROM render_jobs WHERE id = :i"), {"i": str(seed.render_id)})
        await s.execute(text("DELETE FROM timelines WHERE id = :i"), {"i": str(seed.timeline_id)})
        await s.execute(
            text("DELETE FROM media_assets WHERE id IN (:a, :b)"),
            {"a": str(seed.master_id), "b": str(seed.delivery_id)},
        )
        await s.execute(text("DELETE FROM projects WHERE id = :i"), {"i": str(seed.project_id)})
        if drop_identity:
            await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(seed.user_id)})
            await s.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": str(seed.tenant_id)})
        await s.commit()


@asynccontextmanager
async def _seeded(
    session_factory: async_sessionmaker[AsyncSession], **kwargs: object
) -> AsyncIterator[_Seed]:
    seed = await _insert_seed(
        session_factory,
        tenant_id=uuid4(),
        user_id=uuid4(),
        seed_identity=True,
        **kwargs,  # type: ignore[arg-type]
    )
    try:
        yield seed
    finally:
        await _cleanup(session_factory, seed, drop_identity=True)


def _use_case(session_factory: async_sessionmaker[AsyncSession]) -> GeneratePublishMetadata:
    return GeneratePublishMetadata(
        uow=SqlAlchemyUnitOfWork(session_factory),
        generator=LlmPublishMetadataGenerator(default_registry(), timeout_seconds=15.0),
    )


class _FailingGenerator(IPublishMetadataGenerator):
    async def generate(self, req: PublishMetadataRequest) -> GeneratedPublishMetadata:
        raise PublishMetadataGenerationError("forced failure")


# --------------------------------------------------------------------------- #
# Use case against live PostgreSQL                                             #
# --------------------------------------------------------------------------- #
async def test_suggest_returns_deterministic_llm_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _seeded(session_factory, project_name="Mountain Hike Vlog") as seed:
        use_case = _use_case(session_factory)
        first = await use_case.execute(
            tenant_id=seed.tenant_id,
            owner_user_id=seed.user_id,
            export_job_id=seed.export_id,
            request_id="req-1",
        )
        second = await use_case.execute(
            tenant_id=seed.tenant_id,
            owner_user_id=seed.user_id,
            export_job_id=seed.export_id,
            request_id="req-1",
        )

    assert first == second  # deterministic over the mock LLM
    assert first.provenance.generator == "llm"
    assert first.provenance.is_fallback is False
    assert first.title
    assert len(first.title) <= 100


async def test_owner_isolation_unknown_owner_is_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _seeded(session_factory) as seed:
        use_case = _use_case(session_factory)
        with pytest.raises(NotFoundError):
            await use_case.execute(
                tenant_id=seed.tenant_id,
                owner_user_id=uuid4(),  # a different principal cannot see it
                export_job_id=seed.export_id,
                request_id="req-1",
            )


async def test_not_ready_export_is_validation_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _seeded(session_factory, export_status=ExportStatus.RUNNING.value) as seed:
        use_case = _use_case(session_factory)
        with pytest.raises(ValidationFailedError):
            await use_case.execute(
                tenant_id=seed.tenant_id,
                owner_user_id=seed.user_id,
                export_job_id=seed.export_id,
                request_id="req-1",
            )


async def test_generator_failure_falls_back_to_template(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _seeded(session_factory, project_name="Fallback Live") as seed:
        use_case = GeneratePublishMetadata(
            uow=SqlAlchemyUnitOfWork(session_factory), generator=_FailingGenerator()
        )
        result = await use_case.execute(
            tenant_id=seed.tenant_id,
            owner_user_id=seed.user_id,
            export_job_id=seed.export_id,
            request_id="req-1",
        )

    assert result.provenance.generator == "template"
    assert result.provenance.is_fallback is True
    assert result.title == "Fallback Live"
    assert result.tags == ()


# --------------------------------------------------------------------------- #
# HTTP surface (through the real endpoint)                                     #
# --------------------------------------------------------------------------- #
async def test_endpoint_end_to_end_suggestion_for_owner(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"aim-api-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="AIM API",
    )
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}
    seed = await _insert_seed(
        session_factory,
        tenant_id=result.user.tenant_id,
        user_id=result.user.id,
        seed_identity=False,
        project_name="Endpoint Vlog",
    )
    try:
        r = await client.post(
            "/api/v1/publish-metadata/suggestions",
            headers=auth,
            json={"export_job_id": str(seed.export_id)},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["title"]
        assert data["provenance"]["generator"] == "llm"
        assert data["provenance"]["is_fallback"] is False
        assert isinstance(data["tags"], list)
    finally:
        await _cleanup(session_factory, seed, drop_identity=False)


async def test_endpoint_requires_auth(client: AsyncClient) -> None:
    r = await client.post(
        "/api/v1/publish-metadata/suggestions", json={"export_job_id": str(uuid4())}
    )
    assert r.status_code == 401


async def test_endpoint_unknown_export_is_404(client: AsyncClient) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"aim-404-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="AIM 404",
    )
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}
    r = await client.post(
        "/api/v1/publish-metadata/suggestions",
        headers=auth,
        json={"export_job_id": str(uuid4())},
    )
    assert r.status_code == 404, r.text


async def test_endpoint_malformed_body_is_422(client: AsyncClient) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"aim-422-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="AIM 422",
    )
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}
    assert (
        await client.post("/api/v1/publish-metadata/suggestions", headers=auth, json={})
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/publish-metadata/suggestions",
            headers=auth,
            json={"export_job_id": "not-a-uuid"},
        )
    ).status_code == 422
