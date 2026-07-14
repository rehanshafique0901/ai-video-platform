"""Integration tests for ``/api/v1/projects/{id}/timeline`` (α6.3a).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``TimelineRepository``, and the live database.
Every test uses the SAVEPOINT-rolled-back ``client`` fixture; nothing persists.

The Timeline is a **self-contained OCC aggregate** (ADR-0038): ``timelines.version``
fences the whole tree (root + tracks). The wire ``TrackPublic`` carries **no
``version``** — the aggregate token travels in the response ``meta.timeline_version``.
Endpoints are **project-nested** (Q4): a missing/foreign project or an
un-provisioned timeline is a ``404``. A ``z_index`` collision is a ``409``; a stale
timeline version on a fenced write is a ``412``.

Coverage map (α6.3 pre-flight §5.3):

* A1  provision happy               → 201 + ``TimelinePublic`` (version=1, tracks=[])
* A2  provision without auth        → 401
* A3  provision second time         → 409
* A4  provision unknown project     → 404
* A5  provision aspect default from project orientation → 201
* A6  get happy (with tracks)       → 200, ordered by z_index
* A7  get un-provisioned timeline   → 404
* A8  patch timeline happy          → 200, version bump; projects.version untouched
* A9  patch timeline stale / empty  → 412 / 422
* A10 create track happy (no version) → 201 + meta.timeline_version, no track `version`
* A11 create track z_index collision → 409
* A12 create track stale version    → 412
* A13 list tracks ordered           → 200 + meta.timeline_version
* A14 patch track happy / stale     → 200 / 412
* A15 delete track happy / idempotent-by-404 / missing version query → 204 / 404 / 422
* A16 cross-owner isolation         → 404
* A17 create clip happy (no version) → 201 + meta.timeline_version, no clip `version`
* A18 create clip bad time range     → 422 (end <= start)
* A19 create clip unknown media_asset_id → 422; valid link → 201
* A20 create clip stale version      → 412
* A21 list clips ordered by start_seconds → 200 + meta.timeline_version
* A22 get clip happy / cross-track   → 200 / 404
* A23 patch clip happy / stale / partial-range → 200 / 412 / 422
* A24 delete clip happy / idempotent-by-404 / missing version → 204 / 404 / 422
* A25 composition tree embeds clips per track (GET timeline / GET tracks)
* A26 clip endpoints unknown track   → 404
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_TIMELINE_KEYS = {
    "id",
    "project_id",
    "project_version_id",
    "aspect_ratio",
    "frame_rate",
    "background_color",
    "duration_seconds",
    "version",
    "created_at",
    "updated_at",
    "tracks",
}
_TRACK_KEYS = {
    "id",
    "timeline_id",
    "kind",
    "z_index",
    "name",
    "locked",
    "muted",
    "created_at",
    "updated_at",
    "clips",
}
_CLIP_KEYS = {
    "id",
    "track_id",
    "media_asset_id",
    "start_seconds",
    "end_seconds",
    "source_start_seconds",
    "source_end_seconds",
    "volume",
    "locked",
    "transition_in_id",
    "transition_out_id",
    "effects",
    "created_at",
    "updated_at",
}
_CHECKSUM_HEX = "ab" * 32


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"tl-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "T",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _create_project(
    client: AsyncClient, access: str, *, aspect_ratio: str = "horizontal"
) -> str:
    r = await client.post(
        "/api/v1/projects",
        headers=_auth(access),
        json={"name": f"Project {uuid4()}", "aspect_ratio": aspect_ratio},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


async def _provision(client: AsyncClient, access: str, project_id: str, **over: object) -> dict:
    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline",
        headers=_auth(access),
        json=dict(over),
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _create_track(client: AsyncClient, access: str, project_id: str, **over: object) -> dict:
    body: dict = {"kind": "video", "z_index": 0, "name": "Track"}
    body.update(over)
    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline/tracks",
        headers=_auth(access),
        json=body,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _create_clip(
    client: AsyncClient, access: str, project_id: str, track_id: str, **over: object
) -> dict:
    body: dict = {"start_seconds": 0.0, "end_seconds": 5.0}
    body.update(over)
    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips",
        headers=_auth(access),
        json=body,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _register_media(client: AsyncClient, access: str) -> str:
    r = await client.post(
        "/api/v1/media",
        headers=_auth(access),
        json={
            "kind": "video",
            "source": "uploaded",
            "storage_backend": "s3",
            "storage_bucket": "assets",
            "storage_key": f"clips/{uuid4()}.mp4",
            "mime_type": "video/mp4",
            "size_bytes": 4096,
            "checksum_sha256": _CHECKSUM_HEX,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# ---- A1 — provision happy ---------------------------------------------


@pytest.mark.integration
async def test_a1_provision_happy(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)

    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline",
        headers=_auth(access),
        json={"aspect_ratio": "16:9", "frame_rate": 24},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    data = body["data"]
    assert set(data.keys()) == _TIMELINE_KEYS
    assert data["version"] == 1
    assert data["aspect_ratio"] == "16:9"
    assert data["frame_rate"] == 24
    assert data["project_version_id"] is None
    assert data["tracks"] == []


# ---- A2 — provision without auth --------------------------------------


@pytest.mark.integration
async def test_a2_provision_without_auth(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)

    r = await client.post(f"/api/v1/projects/{project_id}/timeline", json={})
    assert r.status_code == 401, r.text


# ---- A3 — provision twice → 409 ---------------------------------------


@pytest.mark.integration
async def test_a3_provision_second_time_conflicts(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)

    r = await client.post(f"/api/v1/projects/{project_id}/timeline", headers=_auth(access), json={})
    assert r.status_code == 409, r.text


# ---- A4 — provision unknown project → 404 -----------------------------


@pytest.mark.integration
async def test_a4_provision_unknown_project(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]

    r = await client.post(f"/api/v1/projects/{uuid4()}/timeline", headers=_auth(access), json={})
    assert r.status_code == 404, r.text


# ---- A5 — aspect default from project orientation ---------------------


@pytest.mark.integration
async def test_a5_aspect_default_from_orientation(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access, aspect_ratio="vertical")

    data = await _provision(client, access, project_id)
    assert data["aspect_ratio"] == "9:16"


# ---- A6 — get happy with ordered tracks -------------------------------


@pytest.mark.integration
async def test_a6_get_happy_ordered_tracks(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)
    await _create_track(client, access, project_id, z_index=2, name="B")
    await _create_track(client, access, project_id, z_index=0, name="A")

    r = await client.get(f"/api/v1/projects/{project_id}/timeline", headers=_auth(access))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [t["z_index"] for t in data["tracks"]] == [0, 2]
    assert set(data["tracks"][0].keys()) == _TRACK_KEYS
    assert "version" not in data["tracks"][0]


# ---- A7 — get un-provisioned timeline → 404 ---------------------------


@pytest.mark.integration
async def test_a7_get_unprovisioned_timeline(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)

    r = await client.get(f"/api/v1/projects/{project_id}/timeline", headers=_auth(access))
    assert r.status_code == 404, r.text


# ---- A8 — patch timeline happy ----------------------------------------


@pytest.mark.integration
async def test_a8_patch_timeline_happy(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)

    # Capture the project version before the timeline edit.
    proj_before = (await client.get(f"/api/v1/projects/{project_id}", headers=_auth(access))).json()
    proj_version_before = proj_before["data"]["version"]

    r = await client.patch(
        f"/api/v1/projects/{project_id}/timeline",
        headers=_auth(access),
        json={"version": 1, "frame_rate": 60},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["frame_rate"] == 60
    assert data["version"] == 2

    # ADR-0035/0038: timeline edit does NOT bump projects.version.
    proj_after = (await client.get(f"/api/v1/projects/{project_id}", headers=_auth(access))).json()
    assert proj_after["data"]["version"] == proj_version_before


# ---- A9 — patch stale / empty → 412 / 422 -----------------------------


@pytest.mark.integration
async def test_a9_patch_timeline_stale_and_empty(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)

    stale = await client.patch(
        f"/api/v1/projects/{project_id}/timeline",
        headers=_auth(access),
        json={"version": 99, "frame_rate": 60},
    )
    assert stale.status_code == 412, stale.text

    empty = await client.patch(
        f"/api/v1/projects/{project_id}/timeline",
        headers=_auth(access),
        json={"version": 1},
    )
    assert empty.status_code == 422, empty.text


# ---- A10 — create track happy (no version) ----------------------------


@pytest.mark.integration
async def test_a10_create_track_happy_no_version(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)

    body = await _create_track(client, access, project_id, kind="audio", z_index=1, name="Audio")
    data = body["data"]
    assert set(data.keys()) == _TRACK_KEYS
    assert "version" not in data
    assert data["kind"] == "audio"
    assert data["z_index"] == 1
    # Aggregate token surfaced in meta (bumped 1 → 2).
    assert body["meta"]["timeline_version"] == 2


# ---- A11 — create track z_index collision → 409 -----------------------


@pytest.mark.integration
async def test_a11_create_track_z_index_collision(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)
    await _create_track(client, access, project_id, z_index=0)

    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline/tracks",
        headers=_auth(access),
        json={"kind": "video", "z_index": 0, "name": "dup"},
    )
    assert r.status_code == 409, r.text


# ---- A12 — create track stale version → 412 ---------------------------


@pytest.mark.integration
async def test_a12_create_track_stale_version(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)

    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline/tracks",
        headers=_auth(access),
        json={"kind": "video", "z_index": 0, "name": "t", "version": 99},
    )
    assert r.status_code == 412, r.text


# ---- A13 — list tracks ordered + token --------------------------------


@pytest.mark.integration
async def test_a13_list_tracks_ordered(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)
    await _create_track(client, access, project_id, z_index=5, name="hi")
    await _create_track(client, access, project_id, z_index=1, name="lo")

    r = await client.get(f"/api/v1/projects/{project_id}/timeline/tracks", headers=_auth(access))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [t["z_index"] for t in body["data"]] == [1, 5]
    assert body["meta"]["timeline_version"] == 3  # two creates: 1 → 3


# ---- A14 — patch track happy / stale ----------------------------------


@pytest.mark.integration
async def test_a14_patch_track_happy_and_stale(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)
    created = await _create_track(client, access, project_id, z_index=0, name="old")
    track_id = created["data"]["id"]
    token = created["meta"]["timeline_version"]  # 2

    ok = await client.patch(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}",
        headers=_auth(access),
        json={"version": token, "name": "new"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["name"] == "new"
    assert ok.json()["meta"]["timeline_version"] == token + 1

    stale = await client.patch(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}",
        headers=_auth(access),
        json={"version": token, "name": "newer"},  # token now stale
    )
    assert stale.status_code == 412, stale.text


# ---- A15 — delete track happy / idempotent / missing version ----------


@pytest.mark.integration
async def test_a15_delete_track_happy_idempotent_and_missing_version(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)
    created = await _create_track(client, access, project_id, z_index=0)
    track_id = created["data"]["id"]
    token = created["meta"]["timeline_version"]  # 2

    # Missing ?version → 422.
    no_ver = await client.delete(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}", headers=_auth(access)
    )
    assert no_ver.status_code == 422, no_ver.text

    ok = await client.delete(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}?version={token}",
        headers=_auth(access),
    )
    assert ok.status_code == 204, ok.text

    # Repeat delete is idempotent-by-404 (not 412), even with the advanced token.
    repeat = await client.delete(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}?version={token + 1}",
        headers=_auth(access),
    )
    assert repeat.status_code == 404, repeat.text


# ---- A16 — cross-owner isolation → 404 --------------------------------


@pytest.mark.integration
async def test_a16_cross_owner_isolation(client: AsyncClient) -> None:
    owner_access = (await _register(client))["access_token"]
    project_id = await _create_project(client, owner_access)
    await _provision(client, owner_access, project_id)

    other_access = (await _register(client))["access_token"]
    r = await client.get(f"/api/v1/projects/{project_id}/timeline", headers=_auth(other_access))
    assert r.status_code == 404, r.text


# ---- clips (α6.3b) ----------------------------------------------------


async def _setup_track(client: AsyncClient) -> tuple[str, str, str]:
    """Register a user, create a project + timeline + one track. Returns ids + access."""
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)
    created = await _create_track(client, access, project_id, z_index=0)
    return access, project_id, created["data"]["id"]


# ---- A17 — create clip happy (no version) -----------------------------


@pytest.mark.integration
async def test_a17_create_clip_happy_no_version(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)

    body = await _create_clip(
        client, access, project_id, track_id, start_seconds=1.0, end_seconds=4.0
    )
    data = body["data"]
    assert set(data.keys()) == _CLIP_KEYS
    assert "version" not in data
    assert data["start_seconds"] == 1.0
    assert data["end_seconds"] == 4.0
    assert data["media_asset_id"] is None
    assert data["effects"] == []
    # Track create bumped 1 → 2; clip create bumps 2 → 3.
    assert body["meta"]["timeline_version"] == 3


# ---- A18 — create clip bad time range → 422 ---------------------------


@pytest.mark.integration
async def test_a18_create_clip_bad_range(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)

    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips",
        headers=_auth(access),
        json={"start_seconds": 5.0, "end_seconds": 5.0},  # end <= start
    )
    assert r.status_code == 422, r.text


# ---- A19 — create clip media_asset_id validation ----------------------


@pytest.mark.integration
async def test_a19_create_clip_media_asset_validation(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)

    # Unknown asset → 422.
    bad = await client.post(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips",
        headers=_auth(access),
        json={"start_seconds": 0.0, "end_seconds": 5.0, "media_asset_id": str(uuid4())},
    )
    assert bad.status_code == 422, bad.text

    # Valid owned asset → 201.
    media_id = await _register_media(client, access)
    ok = await _create_clip(client, access, project_id, track_id, media_asset_id=media_id)
    assert ok["data"]["media_asset_id"] == media_id


# ---- A20 — create clip stale version → 412 ----------------------------


@pytest.mark.integration
async def test_a20_create_clip_stale_version(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)

    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips",
        headers=_auth(access),
        json={"start_seconds": 0.0, "end_seconds": 5.0, "version": 99},
    )
    assert r.status_code == 412, r.text


# ---- A21 — list clips ordered by start_seconds ------------------------


@pytest.mark.integration
async def test_a21_list_clips_ordered(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)
    await _create_clip(client, access, project_id, track_id, start_seconds=10.0, end_seconds=12.0)
    await _create_clip(client, access, project_id, track_id, start_seconds=0.0, end_seconds=5.0)

    r = await client.get(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips",
        headers=_auth(access),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["start_seconds"] for c in body["data"]] == [0.0, 10.0]
    assert "timeline_version" in body["meta"]


# ---- A22 — get clip happy / cross-track → 200 / 404 -------------------


@pytest.mark.integration
async def test_a22_get_clip_happy_and_cross_track(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)
    other = await _create_track(client, access, project_id, z_index=1)
    other_track_id = other["data"]["id"]
    created = await _create_clip(client, access, project_id, track_id)
    clip_id = created["data"]["id"]

    ok = await client.get(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}",
        headers=_auth(access),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["id"] == clip_id

    # Same clip under a different track → 404.
    cross = await client.get(
        f"/api/v1/projects/{project_id}/timeline/tracks/{other_track_id}/clips/{clip_id}",
        headers=_auth(access),
    )
    assert cross.status_code == 404, cross.text


# ---- A23 — patch clip happy / stale / partial-range -------------------


@pytest.mark.integration
async def test_a23_patch_clip_happy_stale_and_bad_range(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)
    created = await _create_clip(
        client, access, project_id, track_id, start_seconds=0.0, end_seconds=5.0
    )
    clip_id = created["data"]["id"]
    token = created["meta"]["timeline_version"]

    ok = await client.patch(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}",
        headers=_auth(access),
        json={"version": token, "volume": 2.0},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["volume"] == 2.0
    assert ok.json()["meta"]["timeline_version"] == token + 1

    # Stale token → 412.
    stale = await client.patch(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}",
        headers=_auth(access),
        json={"version": token, "volume": 3.0},
    )
    assert stale.status_code == 412, stale.text

    # Partial patch that violates the merged range (start beyond stored end) → 422.
    bad = await client.patch(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}",
        headers=_auth(access),
        json={"version": token + 1, "start_seconds": 9.0},  # stored end == 5.0
    )
    assert bad.status_code == 422, bad.text


# ---- A24 — delete clip happy / idempotent / missing version -----------


@pytest.mark.integration
async def test_a24_delete_clip_happy_idempotent_and_missing_version(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)
    created = await _create_clip(client, access, project_id, track_id)
    clip_id = created["data"]["id"]
    token = created["meta"]["timeline_version"]

    # Missing ?version → 422.
    no_ver = await client.delete(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}",
        headers=_auth(access),
    )
    assert no_ver.status_code == 422, no_ver.text

    ok = await client.delete(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}?version={token}",
        headers=_auth(access),
    )
    assert ok.status_code == 204, ok.text

    # Repeat delete is idempotent-by-404 (not 412), even with the advanced token.
    repeat = await client.delete(
        f"/api/v1/projects/{project_id}/timeline/tracks/{track_id}/clips/{clip_id}"
        f"?version={token + 1}",
        headers=_auth(access),
    )
    assert repeat.status_code == 404, repeat.text


# ---- A25 — composition tree embeds clips per track --------------------


@pytest.mark.integration
async def test_a25_composition_tree_embeds_clips(client: AsyncClient) -> None:
    access, project_id, track_id = await _setup_track(client)
    await _create_clip(client, access, project_id, track_id, start_seconds=0.0, end_seconds=5.0)
    await _create_clip(client, access, project_id, track_id, start_seconds=5.0, end_seconds=9.0)

    # GET /timeline embeds each track's clips (ordered).
    tl = await client.get(f"/api/v1/projects/{project_id}/timeline", headers=_auth(access))
    assert tl.status_code == 200, tl.text
    track = tl.json()["data"]["tracks"][0]
    assert [c["start_seconds"] for c in track["clips"]] == [0.0, 5.0]
    assert set(track["clips"][0].keys()) == _CLIP_KEYS

    # GET /tracks embeds them too.
    tr = await client.get(f"/api/v1/projects/{project_id}/timeline/tracks", headers=_auth(access))
    assert tr.status_code == 200, tr.text
    assert len(tr.json()["data"][0]["clips"]) == 2


# ---- A26 — clip endpoints unknown track → 404 -------------------------


@pytest.mark.integration
async def test_a26_clip_endpoints_unknown_track(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision(client, access, project_id)

    r = await client.get(
        f"/api/v1/projects/{project_id}/timeline/tracks/{uuid4()}/clips",
        headers=_auth(access),
    )
    assert r.status_code == 404, r.text
