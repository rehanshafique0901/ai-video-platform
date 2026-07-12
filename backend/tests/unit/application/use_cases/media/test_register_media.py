"""Unit tests for ``RegisterMedia`` (Slice α6.2).

Coverage map (α6.2 pre-flight §5.1):

* U1 — happy path: registers an owner-level asset (no links), commits once,
  returns the entity with server ids; does NOT bump ``projects.version``.
* U2 — project link valid: a live owned project is accepted + echoed.
* U3 — project link foreign / unknown → ``ValidationFailedError`` (422), no write.
* U4 — scene link valid (with its project) is accepted + echoed.
* U5 — scene link WITHOUT a project → ``ValidationFailedError`` (422).
* U6 — scene link foreign / unknown → ``ValidationFailedError`` (422).
* U7 — prompt link valid (with its project) is accepted + echoed.
* U8 — model link valid: a linkable ``model_id`` is accepted + echoed.
* U9 — model link unknown / retired → ``ValidationFailedError`` (422), no write.
* U10 — duplicate ``(backend, bucket, key)`` → ``ConflictError`` (409).
* U11 — ``media.registered`` (INFO) emitted with the field set; storage_key /
  checksum are not logged.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog

from app.application.use_cases.media.register_media import RegisterMedia
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.media.media_asset import MediaAsset
from tests.unit.application.use_cases.media._helpers import (
    build_env,
    media_kwargs,
    register_model,
    seed_prompt,
    seed_scene,
)

# ``project not owned`` is a 422 here (bad link in the body), NOT a 404 — media
# has no project route gate. NotFoundError is imported only to assert it is NOT
# raised on the link path.
_ = NotFoundError


@pytest.mark.unit
async def test_u1_happy_path_owner_level_asset_commits() -> None:
    env = build_env()
    uc = RegisterMedia(uow=env.uow)

    media = await uc.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(kind="video", source="stock"),
    )

    assert isinstance(media, MediaAsset)
    assert media.owner_user_id == env.owner_user_id
    assert media.tenant_id == env.tenant_id
    assert media.project_id is None
    assert media.scene_id is None
    assert media.prompt_id is None
    assert media.model_id is None
    assert media.kind == "video"
    assert media.source == "stock"
    assert media.source_metadata == {}
    assert env.uow.commits == 1
    # ADR-0037: registering media does NOT bump the project OCC token.
    assert env.projects._rows[env.project_id].version == 1


@pytest.mark.unit
async def test_u2_project_link_valid_is_accepted() -> None:
    env = build_env()
    uc = RegisterMedia(uow=env.uow)

    media = await uc.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(project_id=env.project_id),
    )

    assert media.project_id == env.project_id
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u3_foreign_project_link_raises_422_no_write() -> None:
    env = build_env()
    uc = RegisterMedia(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            **media_kwargs(project_id=uuid4()),  # not the caller's project
        )

    assert env.uow.commits == 0
    assert env.media._media == {}


@pytest.mark.unit
async def test_u4_scene_link_valid_is_accepted() -> None:
    env = build_env()
    scene_id = await seed_scene(env)
    uc = RegisterMedia(uow=env.uow)

    media = await uc.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(project_id=env.project_id, scene_id=scene_id),
    )

    assert media.scene_id == scene_id
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u5_scene_link_without_project_raises_422() -> None:
    env = build_env()
    scene_id = await seed_scene(env)
    uc = RegisterMedia(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            **media_kwargs(scene_id=scene_id),  # scene without project
        )

    assert env.uow.commits == 0
    assert env.media._media == {}


@pytest.mark.unit
async def test_u6_foreign_scene_link_raises_422() -> None:
    env = build_env()
    uc = RegisterMedia(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            **media_kwargs(project_id=env.project_id, scene_id=uuid4()),
        )

    assert env.uow.commits == 0


@pytest.mark.unit
async def test_u7_prompt_link_valid_is_accepted() -> None:
    env = build_env()
    prompt_id = await seed_prompt(env)
    uc = RegisterMedia(uow=env.uow)

    media = await uc.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(project_id=env.project_id, prompt_id=prompt_id),
    )

    assert media.prompt_id == prompt_id
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u8_model_link_valid_is_accepted() -> None:
    env = build_env()
    model_id = register_model(env)
    uc = RegisterMedia(uow=env.uow)

    media = await uc.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(model_id=model_id),
    )

    assert media.model_id == model_id
    assert env.uow.commits == 1


@pytest.mark.unit
async def test_u9_unknown_model_link_raises_422_no_write() -> None:
    env = build_env()
    uc = RegisterMedia(uow=env.uow)

    with pytest.raises(ValidationFailedError):
        await uc.execute(
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            **media_kwargs(model_id=uuid4()),  # unknown / retired
        )

    assert env.uow.commits == 0
    assert env.media._media == {}


@pytest.mark.unit
async def test_u10_duplicate_storage_coords_raises_409() -> None:
    env = build_env()
    uc = RegisterMedia(uow=env.uow)

    await uc.execute(
        owner_user_id=env.owner_user_id,
        tenant_id=env.tenant_id,
        **media_kwargs(storage_key="dup/key.png"),
    )
    with pytest.raises(ConflictError):
        await uc.execute(
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            **media_kwargs(storage_key="dup/key.png"),  # same coords
        )

    assert env.uow.commits == 1  # only the first registration committed
    assert len(env.media._media) == 1


@pytest.mark.unit
async def test_u11_registered_log_omits_storage_key_and_checksum() -> None:
    env = build_env()
    uc = RegisterMedia(uow=env.uow)

    with structlog.testing.capture_logs() as logs:
        media = await uc.execute(
            owner_user_id=env.owner_user_id,
            tenant_id=env.tenant_id,
            **media_kwargs(storage_key="secret/path/object.png"),
            ip="203.0.113.7",
        )

    registered = [e for e in logs if e.get("event") == "media.registered"]
    assert len(registered) == 1
    ev = registered[0]
    assert ev["log_level"] == "info"
    assert ev["media_id"] == str(media.id)
    assert ev["kind"] == "image"
    assert ev["source"] == "uploaded"
    assert ev["storage_backend"] == "s3"
    assert ev["ip"] == "203.0.113.7"
    # storage_key + checksum are not logged.
    assert "secret/path/object.png" not in str(ev)
    assert "checksum" not in ev
