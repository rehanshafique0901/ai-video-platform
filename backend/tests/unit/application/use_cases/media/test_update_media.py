"""Unit tests for ``UpdateMedia`` (Slice α6.2, narrow PATCH).

Coverage map (α6.2 pre-flight §5.1):

* U1 — mutate ``source_metadata`` (real change): commits once, ``updated_at``
  advances; no ``projects.version`` bump.
* U2 — re-link ``project_id`` valid: accepted + committed.
* U3 — re-link ``project_id`` foreign → ``ValidationFailedError`` (422).
* U4 — clearing ``project_id`` while a ``scene_id`` remains → 422 (effective
  inconsistency).
* U5 — re-link ``model_id`` unknown → ``ValidationFailedError`` (422).
* U6 — same-value patch → no-op (no commit, unchanged entity).
* U7 — unknown id → ``NotFoundError`` (404).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.media.register_media import RegisterMedia
from app.application.use_cases.media.update_media import UpdateMedia
from app.core.errors import NotFoundError, ValidationFailedError
from tests.unit.application.use_cases.media._helpers import (
    build_env,
    media_kwargs,
    register_model,
    seed_scene,
)


@pytest.mark.unit
async def test_u1_mutate_source_metadata_commits_no_version_bump() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )
    commits_before = env.uow.commits

    updated = await UpdateMedia(uow=env.uow).execute(
        media_id=media.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        changes={"source_metadata": {"origin": "unsplash"}},
    )

    assert updated.source_metadata == {"origin": "unsplash"}
    assert env.uow.commits == commits_before + 1
    assert env.projects._rows[env.project_id].version == 1  # ADR-0037: no bump


@pytest.mark.unit
async def test_u2_relink_project_valid() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )

    updated = await UpdateMedia(uow=env.uow).execute(
        media_id=media.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        changes={"project_id": env.project_id},
    )

    assert updated.project_id == env.project_id


@pytest.mark.unit
async def test_u3_relink_project_foreign_raises_422() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )

    with pytest.raises(ValidationFailedError):
        await UpdateMedia(uow=env.uow).execute(
            media_id=media.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            changes={"project_id": uuid4()},
        )


@pytest.mark.unit
async def test_u4_clear_project_with_scene_remaining_raises_422() -> None:
    env = build_env()
    scene_id = await seed_scene(env)
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(project_id=env.project_id, scene_id=scene_id),
    )

    # Clearing project_id leaves the scene link dangling → effective set invalid.
    with pytest.raises(ValidationFailedError):
        await UpdateMedia(uow=env.uow).execute(
            media_id=media.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            changes={"project_id": None},
        )


@pytest.mark.unit
async def test_u5_relink_model_unknown_raises_422() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )

    with pytest.raises(ValidationFailedError):
        await UpdateMedia(uow=env.uow).execute(
            media_id=media.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            changes={"model_id": uuid4()},  # unknown / retired
        )


@pytest.mark.unit
async def test_u6_same_value_patch_is_noop() -> None:
    env = build_env()
    model_id = register_model(env)
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(model_id=model_id),
    )
    commits_before = env.uow.commits

    result = await UpdateMedia(uow=env.uow).execute(
        media_id=media.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        changes={"model_id": model_id},  # already this value
    )

    assert result.id == media.id
    assert env.uow.commits == commits_before  # no write, no commit


@pytest.mark.unit
async def test_u7_unknown_id_raises_404() -> None:
    env = build_env()
    with pytest.raises(NotFoundError):
        await UpdateMedia(uow=env.uow).execute(
            media_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            changes={"source_metadata": {"x": 1}},
        )
