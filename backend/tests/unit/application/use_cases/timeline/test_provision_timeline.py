"""Unit tests for ``ProvisionTimeline`` (Slice α6.3a).

Coverage map (α6.3 pre-flight):

* U1 — happy path: creates the single timeline (``version = 1``, no tracks),
  commits once; does NOT bump ``projects.version`` (ADR-0035/ADR-0038).
* U2 — ``aspect_ratio`` defaults from the project orientation when omitted.
* U3 — an explicit ``aspect_ratio`` overrides the project default.
* U4 — unknown project (not the caller's) → ``NotFoundError`` (404), no write.
* U5 — a second provision for the same project → ``ConflictError`` (409).
* U6 — ``timeline.provisioned`` (INFO) emitted with the field set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.timeline.provision_timeline import ProvisionTimeline
from app.application.use_cases.timeline.results import TimelineResult
from app.core.errors import ConflictError, NotFoundError
from tests.unit.application.use_cases.timeline._helpers import build_env, seed_timeline


@pytest.mark.unit
async def test_u1_happy_path_creates_version_1_no_tracks() -> None:
    env = build_env()
    uc = ProvisionTimeline(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        aspect_ratio="16:9",
    )

    assert isinstance(result, TimelineResult)
    assert result.timeline.project_id == env.project_id
    assert result.timeline.version == 1
    assert result.timeline.project_version_id is None
    assert result.tracks == []
    assert env.uow.commits == 1
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_aspect_ratio_defaults_from_project_orientation() -> None:
    env = build_env(aspect_ratio="vertical")
    uc = ProvisionTimeline(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
    )

    assert result.timeline.aspect_ratio == "9:16"


@pytest.mark.unit
async def test_u3_explicit_aspect_ratio_overrides_default() -> None:
    env = build_env(aspect_ratio="horizontal")
    uc = ProvisionTimeline(uow=env.uow)

    result = await uc.execute(
        project_id=env.project_id,
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        aspect_ratio="21:9",
    )

    assert result.timeline.aspect_ratio == "21:9"


@pytest.mark.unit
async def test_u4_unknown_project_raises_404_no_write() -> None:
    env = build_env()
    uc = ProvisionTimeline(uow=env.uow)

    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=uuid4(),  # not the caller's project
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )

    assert env.uow.commits == 0
    assert env.timeline._timelines == {}


@pytest.mark.unit
async def test_u5_second_provision_raises_409() -> None:
    env = build_env()
    await seed_timeline(env)
    uc = ProvisionTimeline(uow=env.uow)

    with pytest.raises(ConflictError):
        await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
        )

    assert env.uow.commits == 0
    assert len(env.timeline._timelines) == 1


@pytest.mark.unit
async def test_u6_provisioned_log_emitted() -> None:
    env = build_env()
    uc = ProvisionTimeline(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        result = await uc.execute(
            project_id=env.project_id,
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            aspect_ratio="16:9",
            ip="203.0.113.7",
        )

    events = [e for e in logs if e.get("event") == "timeline.provisioned"]
    assert len(events) == 1
    ev = events[0]
    assert ev["log_level"] == "info"
    assert ev["timeline_id"] == str(result.timeline.id)
    assert ev["project_id"] == str(env.project_id)
    assert ev["aspect_ratio"] == "16:9"
    assert ev["ip"] == "203.0.113.7"
