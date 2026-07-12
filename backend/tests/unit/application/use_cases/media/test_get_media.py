"""Unit tests for ``GetMedia`` (Slice α6.2).

Coverage map (α6.2 pre-flight §5.1):

* U1 — happy path: returns the owner's live asset.
* U2 — unknown id → ``NotFoundError`` (404).
* U3 — another owner's asset → ``NotFoundError`` (404, indistinguishable).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.media.get_media import GetMedia
from app.application.use_cases.media.register_media import RegisterMedia
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.media._helpers import build_env, media_kwargs


@pytest.mark.unit
async def test_u1_happy_path_returns_owned_asset() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )

    got = await GetMedia(uow=env.uow).execute(
        media_id=media.id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert got.id == media.id


@pytest.mark.unit
async def test_u2_unknown_id_raises_404() -> None:
    env = build_env()
    with pytest.raises(NotFoundError):
        await GetMedia(uow=env.uow).execute(
            media_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u3_other_owner_raises_404() -> None:
    env = build_env()
    media = await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )

    with pytest.raises(NotFoundError):
        await GetMedia(uow=env.uow).execute(
            media_id=media.id,
            owner_user_id=uuid4(),  # not the owner
            tenant_id=env.tenant_id,
        )
