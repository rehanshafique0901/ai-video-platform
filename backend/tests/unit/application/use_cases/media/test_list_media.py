"""Unit tests for ``ListMedia`` (Slice α6.2).

Coverage map (α6.2 pre-flight §5.1):

* U1 — newest-first ordering (insertion ordinal DESC mirrors (created_at, id)).
* U2 — ``kind`` / ``source`` filters narrow the result (AND).
* U3 — ``project_id`` / ``scene_id`` filters narrow the result.
* U4 — owner isolation: another owner's assets are never listed.
* U5 — empty → ``[]``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.media.list_media import ListMedia
from app.application.use_cases.media.register_media import RegisterMedia
from tests.unit.application.use_cases.media._helpers import build_env, media_kwargs


@pytest.mark.unit
async def test_u1_newest_first() -> None:
    env = build_env()
    reg = RegisterMedia(uow=env.uow)
    first = await reg.execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )
    second = await reg.execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )

    listed = await ListMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id
    )

    assert [m.id for m in listed] == [second.id, first.id]


@pytest.mark.unit
async def test_u2_kind_and_source_filters() -> None:
    env = build_env()
    reg = RegisterMedia(uow=env.uow)
    await reg.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(kind="image", source="uploaded"),
    )
    await reg.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(kind="video", source="stock"),
    )

    uc = ListMedia(uow=env.uow)
    by_kind = await uc.execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, kind="video"
    )
    by_source = await uc.execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, source="stock"
    )
    combined = await uc.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        kind="image",
        source="stock",
    )

    assert [m.kind for m in by_kind] == ["video"]
    assert [m.source for m in by_source] == ["stock"]
    assert combined == []  # image+stock: no such asset


@pytest.mark.unit
async def test_u3_project_and_scene_filters() -> None:
    env = build_env()
    reg = RegisterMedia(uow=env.uow)
    linked = await reg.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(project_id=env.project_id),
    )
    await reg.execute(owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs())

    by_project = await ListMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        project_id=env.project_id,
    )

    assert [m.id for m in by_project] == [linked.id]


@pytest.mark.unit
async def test_u4_owner_isolation() -> None:
    env = build_env()
    await RegisterMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id, **media_kwargs()
    )

    other = await ListMedia(uow=env.uow).execute(
        owner_user_id=uuid4(),  # a different owner
        tenant_id=env.tenant_id,
    )

    assert other == []


@pytest.mark.unit
async def test_u5_empty_returns_empty_list() -> None:
    env = build_env()
    listed = await ListMedia(uow=env.uow).execute(
        owner_user_id=env.owner_user_id, tenant_id=env.tenant_id
    )
    assert listed == []
