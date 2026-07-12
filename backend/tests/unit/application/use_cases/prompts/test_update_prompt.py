"""Unit tests for ``UpdatePrompt`` (Slice α6.1).

Coverage map (α6.1 pre-flight §5.1): real change (no OCC bump) · same-value
no-op (no write) · prompt not visible → 404 · unknown model → 422 ·
explicit ``model_id: null`` clears the link.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.prompts.create_prompt import CreatePrompt
from app.application.use_cases.prompts.update_prompt import UpdatePrompt
from app.core.errors import NotFoundError, ValidationFailedError
from tests.unit.application.use_cases.prompts._helpers import build_env, register_model


async def _make(env, **kw):  # type: ignore[no-untyped-def]
    return await CreatePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **kw,
    )


@pytest.mark.unit
async def test_u1_real_change_updates_and_no_occ_bump() -> None:
    env = build_env()
    created = await _make(env, kind="image", text_content="before")
    commits_before = env.uow.commits

    updated = await UpdatePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        prompt_id=created.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        changes={"text_content": "after"},
    )

    assert updated.text_content == "after"
    assert env.uow.commits == commits_before + 1
    # ADR-0036 / Q1=A: no aggregate OCC bump on a prompt update.
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_same_value_noop_no_write() -> None:
    env = build_env()
    created = await _make(env, kind="image", text_content="same")
    commits_before = env.uow.commits

    updated = await UpdatePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        prompt_id=created.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        changes={"text_content": "same"},
    )

    assert updated.id == created.id
    assert env.uow.commits == commits_before  # no write


@pytest.mark.unit
async def test_u3_unknown_prompt_raises_404() -> None:
    env = build_env()
    with pytest.raises(NotFoundError):
        await UpdatePrompt(uow=env.uow).execute(
            project_id=env.project_id,
            prompt_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            changes={"text_content": "x"},
        )


@pytest.mark.unit
async def test_u4_unknown_model_raises_422() -> None:
    env = build_env()
    created = await _make(env, kind="image", text_content="x")
    with pytest.raises(ValidationFailedError):
        await UpdatePrompt(uow=env.uow).execute(
            project_id=env.project_id,
            prompt_id=created.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            changes={"model_id": uuid4()},
        )


@pytest.mark.unit
async def test_u5_explicit_null_clears_model_link() -> None:
    env = build_env()
    model_id = register_model(env)
    created = await _make(env, kind="image", text_content="x", model_id=model_id)
    assert created.model_id == model_id

    updated = await UpdatePrompt(uow=env.uow).execute(
        project_id=env.project_id,
        prompt_id=created.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        changes={"model_id": None},
    )

    assert updated.model_id is None
