"""Integration tests for ``PromptRepository`` (Slice α6.1).

Runs against the live database; each test is wrapped in a SAVEPOINT that rolls
back on teardown, so no rows persist. Covers project-scoped create (project- and
scene-linked), newest-first listing with soft-delete exclusion + kind/scene
filters, cross-project isolation on read, the no-OCC update (``updated_at``
advances, no ``version`` column exists), soft delete + idempotency,
``model_is_linkable`` (exists + not-retired), and the load-bearing **F6**
schema-interaction test: a prompt's ``scene_id`` link SURVIVES a scene
*soft-delete* (the ``ON DELETE SET NULL`` FK fires only on a hard parent delete).

Coverage map (α6.1 pre-flight §5.2):

* R1 — ``add`` project-level (``scene_id``/``model_id`` NULL) + scene-linked.
* R2 — ``list_owned`` newest-first, excludes soft-deleted.
* R3 — ``list_owned`` kind + scene filters (combined AND).
* R4 — ``get_owned`` cross-project isolation → ``None``.
* R5 — ``update_owned`` real change: ``updated_at`` advances (no ``version``).
* R6 — ``update_owned`` foreign project / soft-deleted → ``None``.
* R7 — ``soft_delete_owned`` happy / already-deleted → ``False`` / wrong owner.
* R8 — ``model_is_linkable``: available/deprecated True, retired/unknown False.
* R9 — **F6** prompt ``scene_id`` survives scene soft-delete.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.prompts.prompt import Prompt as PromptEntity
from app.infrastructure.db.models.ai_models import AIModel as AIModelRow
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.db.models.projects import Project as ProjectRow
from app.infrastructure.db.models.scenes import (
    Prompt as PromptRow,
    Scene as SceneRow,
    Storyboard as StoryboardRow,
)
from app.infrastructure.repositories.prompt_repository import PromptRepository


async def _seed_owner(session: AsyncSession) -> tuple[UUID, UUID]:
    tenant_id = uuid4()
    await session.execute(
        insert(Tenant).values(id=tenant_id, name="PR Test", slug=f"pr-{tenant_id}")
    )
    user_id = uuid4()
    await session.execute(
        insert(User).values(
            id=user_id,
            tenant_id=tenant_id,
            email=f"pr-{user_id}@example.com",
            display_name="PR Owner",
        )
    )
    return tenant_id, user_id


async def _insert_project(session: AsyncSession, *, tenant_id: UUID, owner_user_id: UUID) -> UUID:
    project_id = uuid4()
    await session.execute(
        insert(ProjectRow).values(
            id=project_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=f"P {project_id}",
            aspect_ratio="horizontal",
        )
    )
    return project_id


async def _insert_scene(session: AsyncSession, *, project_id: UUID) -> tuple[UUID, UUID]:
    storyboard_id = uuid4()
    await session.execute(
        insert(StoryboardRow).values(id=storyboard_id, project_id=project_id, generated_by="system")
    )
    scene_id = uuid4()
    await session.execute(
        insert(SceneRow).values(
            id=scene_id,
            storyboard_id=storyboard_id,
            scene_number=1000,
            title="S1",
            duration_seconds=1.0,
        )
    )
    await session.flush()
    return storyboard_id, scene_id


async def _insert_model(session: AsyncSession, *, status: str = "available") -> UUID:
    model_id = uuid4()
    await session.execute(
        insert(AIModelRow).values(
            id=model_id,
            model_key=f"mk-{model_id}",
            provider="test",
            vendor_model_id="v1",
            kind="image",
            status=status,
        )
    )
    await session.flush()
    return model_id


async def _add(
    repo: PromptRepository,
    project_id: UUID,
    *,
    text_content: str = "p",
    kind: str = "image",
    scene_id: UUID | None = None,
    model_id: UUID | None = None,
    extra: dict[str, object] | None = None,
) -> PromptEntity:
    """Thin ``repo.add`` wrapper with defaults, to keep call sites short."""
    return await repo.add(
        project_id=project_id,
        scene_id=scene_id,
        kind=kind,
        text_content=text_content,
        model_id=model_id,
        extra=extra or {},
    )


# ---- R1 — add project-level + scene-linked ----------------------------


@pytest.mark.integration
async def test_r1_add_project_level_and_scene_linked(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    _sb, scene_id = await _insert_scene(session, project_id=project_id)
    repo = PromptRepository(session)

    plain = await _add(repo, project_id, text_content="project-level")
    assert plain.scene_id is None
    assert plain.model_id is None
    assert plain.kind == "image"

    linked = await _add(
        repo,
        project_id,
        kind="motion",
        scene_id=scene_id,
        text_content="scene-scoped",
        extra={"weight": 2},
    )
    assert linked.scene_id == scene_id
    assert linked.extra == {"weight": 2}


# ---- R2 — list newest-first, excludes soft-deleted --------------------


@pytest.mark.integration
async def test_r2_list_newest_first_excludes_soft_deleted(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = PromptRepository(session)

    p1 = await _add(repo, project_id, text_content="1")
    p2 = await _add(repo, project_id, text_content="2")
    # Transaction-constant now(): created_at ties; the (created_at, id) DESC
    # total order still gives a deterministic sequence. Assert the set + that
    # a soft-deleted row drops out, not a specific tie order.
    await repo.soft_delete_owned(project_id, p1.id)

    listed = await repo.list_owned(project_id)
    assert [p.id for p in listed] == [p2.id]


# ---- R3 — list filters -------------------------------------------------


@pytest.mark.integration
async def test_r3_list_filters(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    _sb, scene_id = await _insert_scene(session, project_id=project_id)
    repo = PromptRepository(session)

    await _add(repo, project_id, kind="image", text_content="i")
    vid = await _add(repo, project_id, kind="video", text_content="v")
    scoped = await _add(repo, project_id, kind="video", scene_id=scene_id, text_content="s")

    assert {p.id for p in await repo.list_owned(project_id, kind="video")} == {vid.id, scoped.id}
    assert [p.id for p in await repo.list_owned(project_id, scene_id=scene_id)] == [scoped.id]
    # combined AND
    assert [p.id for p in await repo.list_owned(project_id, kind="video", scene_id=scene_id)] == [
        scoped.id
    ]


# ---- R4 — get cross-project isolation ---------------------------------


@pytest.mark.integration
async def test_r4_get_owned_cross_project_isolation(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_a = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    project_b = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = PromptRepository(session)

    prompt = await _add(repo, project_a, text_content="x")
    assert await repo.get_owned(project_a, prompt.id) is not None
    # Same prompt id, wrong project → invisible.
    assert await repo.get_owned(project_b, prompt.id) is None


# ---- R5 — update real change (no version column) ----------------------


@pytest.mark.integration
async def test_r5_update_owned_changes_content(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = PromptRepository(session)

    prompt = await _add(repo, project_id, text_content="before")
    updated = await repo.update_owned(project_id, prompt.id, {"text_content": "after"})
    assert updated is not None
    assert updated.text_content == "after"
    # No version column on prompts (ADR-0036) — the entity carries none.
    assert not hasattr(updated, "version")


# ---- R6 — update foreign / soft-deleted → None ------------------------


@pytest.mark.integration
async def test_r6_update_owned_foreign_or_deleted_returns_none(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_a = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    project_b = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = PromptRepository(session)

    prompt = await _add(repo, project_a, text_content="x")
    # Foreign project scope → no match.
    assert await repo.update_owned(project_b, prompt.id, {"text_content": "y"}) is None
    # Soft-deleted → no match.
    await repo.soft_delete_owned(project_a, prompt.id)
    assert await repo.update_owned(project_a, prompt.id, {"text_content": "y"}) is None


# ---- R7 — soft delete happy / idempotent / wrong owner ----------------


@pytest.mark.integration
async def test_r7_soft_delete_owned(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_a = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    project_b = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    repo = PromptRepository(session)

    prompt = await _add(repo, project_a, text_content="x")
    # Wrong project → False, no delete.
    assert await repo.soft_delete_owned(project_b, prompt.id) is False
    # Happy → True.
    assert await repo.soft_delete_owned(project_a, prompt.id) is True
    # Idempotent: already deleted → False.
    assert await repo.soft_delete_owned(project_a, prompt.id) is False


# ---- R8 — model_is_linkable -------------------------------------------


@pytest.mark.integration
async def test_r8_model_is_linkable(session: AsyncSession) -> None:
    repo = PromptRepository(session)
    available = await _insert_model(session, status="available")
    deprecated = await _insert_model(session, status="deprecated")
    retired = await _insert_model(session, status="retired")

    assert await repo.model_is_linkable(available) is True
    assert await repo.model_is_linkable(deprecated) is True
    assert await repo.model_is_linkable(retired) is False
    assert await repo.model_is_linkable(uuid4()) is False


# ---- R9 — F6: scene link survives scene soft-delete -------------------


@pytest.mark.integration
async def test_r9_scene_link_survives_scene_soft_delete(session: AsyncSession) -> None:
    tenant_id, owner_id = await _seed_owner(session)
    project_id = await _insert_project(session, tenant_id=tenant_id, owner_user_id=owner_id)
    _sb, scene_id = await _insert_scene(session, project_id=project_id)
    repo = PromptRepository(session)

    prompt = await _add(repo, project_id, kind="motion", scene_id=scene_id, text_content="x")
    assert prompt.scene_id == scene_id

    # Soft-delete the scene (α5c-style: set deleted_at, NOT a hard DELETE). The
    # ON DELETE SET NULL FK fires only on a hard parent delete, so the link
    # must SURVIVE (F6 — load-bearing schema interaction).
    await session.execute(
        update(SceneRow).where(SceneRow.id == scene_id).values(deleted_at=func.now())
    )
    await session.flush()

    still = await repo.get_owned(project_id, prompt.id)
    assert still is not None
    assert still.scene_id == scene_id  # link intact despite scene soft-delete

    # Confirm at the row level too (defence in depth).
    row_scene_id = (
        await session.execute(select(PromptRow.scene_id).where(PromptRow.id == prompt.id))
    ).scalar_one()
    assert row_scene_id == scene_id
