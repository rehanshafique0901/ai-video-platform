"""Unit tests for ``ListTracks`` (Slice α6.3a).

Coverage map (α6.3 pre-flight):

* U1 — happy path: returns tracks ordered by ``z_index`` ASC + the timeline token.
* U2 — empty timeline: empty list, token still surfaced.
* U3 — unknown project → ``NotFoundError`` (404).
* U4 — project without a timeline → ``NotFoundError`` (404).
* U5 — read-only: no commit.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.timeline.list_tracks import ListTracks
from app.core.errors import NotFoundError
from tests.unit.application.use_cases.timeline._helpers import (
    build_env,
    seed_timeline,
    seed_track,
)


@pytest.mark.unit
async def test_u1_happy_path_ordered_with_token() -> None:
    env = build_env()
    timeline = await seed_timeline(env)
    await seed_track(env, timeline, z_index=5, name="last")
    await seed_track(env, timeline, z_index=1, name="first")
    uc = ListTracks(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert [t.z_index for t in result.tracks] == [1, 5]
    assert result.timeline.version == 1


@pytest.mark.unit
async def test_u2_empty_timeline_returns_empty_list() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = ListTracks(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert result.tracks == []


@pytest.mark.unit
async def test_u3_unknown_project_raises_404() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = ListTracks(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u4_no_timeline_raises_404() -> None:
    env = build_env()
    uc = ListTracks(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )


@pytest.mark.unit
async def test_u5_read_only_no_commit() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = ListTracks(uow=env.uow)

    await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert env.uow.commits == 0
