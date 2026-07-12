"""Unit tests for ``DeleteMedia`` (Slice α6.2).

Coverage map (α6.2 pre-flight §5.1):

* U1 — happy path: soft-deletes the owner's live asset, commits once; does NOT
  bump ``projects.version``.
* U2 — idempotent-by-404: a second delete → ``NotFoundError`` (404).
* U3 — another owner's asset → ``NotFoundError`` (404), no commit.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.media.delete_media import DeleteMedia
from app.application.use_cases.media.register_media import RegisterMedia
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.media._helpers import build_env, media_kwargs


@pytest.mark.unit
async def test_u1_happy_path_soft_deletes_and_commits() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )
    commits_before = env.uow.commits

    await DeleteMedia(uow=env.uow).execute(
        media_id=media.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert env.uow.commits == commits_before + 1
    assert env.media._media == {}  # dropped from the live view
    assert env.projects._rows[env.project_id].version == 1  # ADR-0037: no bump


@pytest.mark.unit
async def test_u2_second_delete_raises_404() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )
    uc = DeleteMedia(uow=env.uow)
    await uc.execute(media_id=media.id, owner_user_id=env.owner_user_id, tenant_id=env.tenant_id)

    with pytest.raises(NotFoundError):
        await uc.execute(
            media_id=media.id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u3_other_owner_raises_404_no_commit() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )
    commits_before = env.uow.commits

    with pytest.raises(NotFoundError):
        await DeleteMedia(uow=env.uow).execute(
            media_id=media.id,
            owner_user_id=uuid4(),  # not the owner
            tenant_id=env.tenant_id,
        )

    assert env.uow.commits == commits_before
