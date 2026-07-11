"""Integration tests for ``/api/v1/projects/{project_id}/scenes`` (α5c).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``SceneRepository`` (+ implicit storyboard),
and the live database. Every test uses the SAVEPOINT-rolled-back ``client``
fixture; nothing persists between tests.

Coverage map (α5c pre-flight §5.3):

* A1  create happy               → 201 + ``ScenePublic`` (version=1, position=1)
* A2  create without auth        → 401
* A3  create on unowned/unknown project → 404
* A4  create invalid duration    → 422
* A5  create extra field         → 422 (``extra="forbid"``)
* A6  create empty title         → 422
* A7  list empty                 → 200, ``data == []``
* A8  list ordered w/ positions  → 200, positions 1..N in scene order
* A9  list unowned project       → 404
* A10 get happy                  → 200, matches created (no storyboard_id/scene_number)
* A11 get unknown scene          → 404
* A12 get another owner's scene  → 404 (two-level gate)
* A13 get non-UUID scene path    → 422
* A14 patch real change          → 200, field changed, version+1
* A15 patch partial preserves    → untouched fields kept
* A16 patch clear nullable / title:null → null / 422
* A17 patch same-value no-op     → 200, version unchanged
* A18 patch stale version        → 412 VERSION_CONFLICT
* A19 patch empty / missing version / forbidden position → 422 each
* A20 move reorders              → 200, new position; list order changes
* A21 move stale version         → 412
* A22 delete happy / idempotent-by-404 / cross-owner → 204 then 404
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_PUBLIC_KEYS = {
    "id",
    "project_id",
    "position",
    "title",
    "duration_seconds",
    "narration",
    "subtitle",
    "created_at",
    "updated_at",
    "version",
}


def _fresh_email() -> str:
    return f"scene-{uuid4()}@example.com"


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": _fresh_email(),
            "password": "correct horse battery staple",
            "name": "S",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _create_project(client: AsyncClient, access: str) -> str:
    r = await client.post(
        "/api/v1/projects",
        headers=_auth(access),
        json={"name": f"Project {uuid4()}", "aspect_ratio": "horizontal"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


async def _create_scene(client: AsyncClient, access: str, project_id: str, **body: object) -> dict:
    payload = {"title": f"Scene {uuid4()}", "duration_seconds": 5.0, **body}
    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes",
        headers=_auth(access),
        json=payload,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---- A1 — create happy -------------------------------------------------


@pytest.mark.integration
async def test_a1_create_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes",
        headers=_auth(access),
        json={"title": "Opening", "duration_seconds": 4.5, "narration": "hi"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]
    data = body["data"]
    assert set(data.keys()) == _PUBLIC_KEYS
    assert data["title"] == "Opening"
    assert data["duration_seconds"] == 4.5
    assert data["narration"] == "hi"
    assert data["position"] == 1
    assert data["version"] == 1
    assert data["project_id"] == project_id
    # Internals never leak (α5c Q6).
    assert "storyboard_id" not in data
    assert "scene_number" not in data


# ---- A2 — create without auth -----------------------------------------


@pytest.mark.integration
async def test_a2_create_without_auth_returns_401(client: AsyncClient) -> None:
    r = await client.post(
        f"/api/v1/projects/{uuid4()}/scenes",
        json={"title": "X", "duration_seconds": 1.0},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


# ---- A3 — create on unowned/unknown project ---------------------------


@pytest.mark.integration
async def test_a3_create_on_unknown_project_returns_404(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.post(
        f"/api/v1/projects/{uuid4()}/scenes",
        headers=_auth(reg["access_token"]),
        json={"title": "X", "duration_seconds": 1.0},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ---- A4–A6 — create validation (422) ----------------------------------


@pytest.mark.integration
async def test_a4_create_invalid_duration_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes",
        headers=_auth(access),
        json={"title": "Bad", "duration_seconds": 0},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.integration
async def test_a5_create_extra_field_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes",
        headers=_auth(access),
        json={"title": "X", "duration_seconds": 1.0, "position": 3},  # server-owned
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.integration
async def test_a6_create_empty_title_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes",
        headers=_auth(access),
        json={"title": "   ", "duration_seconds": 1.0},
    )
    assert r.status_code == 422


# ---- A7–A9 — list ------------------------------------------------------


@pytest.mark.integration
async def test_a7_list_empty(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.get(f"/api/v1/projects/{project_id}/scenes", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


@pytest.mark.integration
async def test_a8_list_ordered_with_positions(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    ids = [(await _create_scene(client, access, project_id, title=f"S{i}"))["id"] for i in range(3)]

    r = await client.get(f"/api/v1/projects/{project_id}/scenes", headers=_auth(access))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [s["id"] for s in data] == ids  # append order
    assert [s["position"] for s in data] == [1, 2, 3]


@pytest.mark.integration
async def test_a9_list_unowned_project_returns_404(client: AsyncClient) -> None:
    reg_a = await _register(client)
    reg_b = await _register(client)
    project_id = await _create_project(client, reg_a["access_token"])
    # B cannot list A's scenes.
    r = await client.get(
        f"/api/v1/projects/{project_id}/scenes", headers=_auth(reg_b["access_token"])
    )
    assert r.status_code == 404


# ---- A10–A13 — get -----------------------------------------------------


@pytest.mark.integration
async def test_a10_get_happy(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene = await _create_scene(client, access, project_id, title="Fetch me")

    r = await client.get(
        f"/api/v1/projects/{project_id}/scenes/{scene['id']}", headers=_auth(access)
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["id"] == scene["id"]
    assert data["title"] == "Fetch me"
    assert set(data.keys()) == _PUBLIC_KEYS


@pytest.mark.integration
async def test_a11_get_unknown_scene_returns_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.get(f"/api/v1/projects/{project_id}/scenes/{uuid4()}", headers=_auth(access))
    assert r.status_code == 404


@pytest.mark.integration
async def test_a12_get_other_owners_scene_returns_404(client: AsyncClient) -> None:
    reg_a = await _register(client)
    reg_b = await _register(client)
    project_id = await _create_project(client, reg_a["access_token"])
    scene = await _create_scene(client, reg_a["access_token"], project_id)
    # B addresses A's project+scene → uniform 404 (two-level gate).
    r = await client.get(
        f"/api/v1/projects/{project_id}/scenes/{scene['id']}",
        headers=_auth(reg_b["access_token"]),
    )
    assert r.status_code == 404


@pytest.mark.integration
async def test_a13_get_non_uuid_scene_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.get(f"/api/v1/projects/{project_id}/scenes/not-a-uuid", headers=_auth(access))
    assert r.status_code == 422


# ---- A14–A19 — patch ---------------------------------------------------


@pytest.mark.integration
async def test_a14_patch_real_change_bumps_version(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene = await _create_scene(client, access, project_id, title="Old")

    r = await client.patch(
        f"/api/v1/projects/{project_id}/scenes/{scene['id']}",
        headers=_auth(access),
        json={"version": 1, "title": "New"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["title"] == "New"
    assert data["version"] == 2


@pytest.mark.integration
async def test_a15_patch_partial_preserves_other_fields(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene = await _create_scene(
        client, access, project_id, title="Keep", narration="orig", duration_seconds=3.0
    )

    r = await client.patch(
        f"/api/v1/projects/{project_id}/scenes/{scene['id']}",
        headers=_auth(access),
        json={"version": 1, "title": "Renamed"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["title"] == "Renamed"
    assert data["narration"] == "orig"  # untouched
    assert data["duration_seconds"] == 3.0


@pytest.mark.integration
async def test_a16_patch_clear_nullable_and_title_null(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene = await _create_scene(client, access, project_id, narration="to clear")

    ok = await client.patch(
        f"/api/v1/projects/{project_id}/scenes/{scene['id']}",
        headers=_auth(access),
        json={"version": 1, "narration": None},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["narration"] is None

    # title is non-nullable → explicit null is a 422.
    bad = await client.patch(
        f"/api/v1/projects/{project_id}/scenes/{scene['id']}",
        headers=_auth(access),
        json={"version": 2, "title": None},
    )
    assert bad.status_code == 422


@pytest.mark.integration
async def test_a17_patch_same_value_noop_keeps_version(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene = await _create_scene(client, access, project_id, title="Same")

    r = await client.patch(
        f"/api/v1/projects/{project_id}/scenes/{scene['id']}",
        headers=_auth(access),
        json={"version": 1, "title": "Same"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["version"] == 1  # unchanged


@pytest.mark.integration
async def test_a18_patch_stale_version_returns_412(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene = await _create_scene(client, access, project_id)

    r = await client.patch(
        f"/api/v1/projects/{project_id}/scenes/{scene['id']}",
        headers=_auth(access),
        json={"version": 99, "title": "Nope"},
    )
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"


@pytest.mark.integration
async def test_a19_patch_empty_missing_version_forbidden_field_422(
    client: AsyncClient,
) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene = await _create_scene(client, access, project_id)
    url = f"/api/v1/projects/{project_id}/scenes/{scene['id']}"

    empty = await client.patch(url, headers=_auth(access), json={"version": 1})
    assert empty.status_code == 422

    missing = await client.patch(url, headers=_auth(access), json={"title": "X"})
    assert missing.status_code == 422

    forbidden = await client.patch(url, headers=_auth(access), json={"version": 1, "position": 2})
    assert forbidden.status_code == 422


# ---- A20–A21 — move ----------------------------------------------------


@pytest.mark.integration
async def test_a20_move_reorders(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    ids = [(await _create_scene(client, access, project_id, title=f"S{i}"))["id"] for i in range(3)]

    # Move first scene to position 3.
    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes/{ids[0]}/move",
        headers=_auth(access),
        json={"version": 1, "position": 3},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["position"] == 3
    assert r.json()["data"]["version"] == 2

    listed = await client.get(f"/api/v1/projects/{project_id}/scenes", headers=_auth(access))
    assert [s["id"] for s in listed.json()["data"]] == [ids[1], ids[2], ids[0]]


@pytest.mark.integration
async def test_a21_move_stale_version_returns_412(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    ids = [(await _create_scene(client, access, project_id, title=f"S{i}"))["id"] for i in range(2)]

    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes/{ids[0]}/move",
        headers=_auth(access),
        json={"version": 99, "position": 2},
    )
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"


# ---- A22 — delete ------------------------------------------------------


@pytest.mark.integration
async def test_a22_delete_happy_idempotent_and_cross_owner(client: AsyncClient) -> None:
    reg_a = await _register(client)
    reg_b = await _register(client)
    access = reg_a["access_token"]
    project_id = await _create_project(client, access)
    scene = await _create_scene(client, access, project_id)
    url = f"/api/v1/projects/{project_id}/scenes/{scene['id']}"

    # Cross-owner delete → 404 (B cannot touch A's scene).
    cross = await client.delete(url, headers=_auth(reg_b["access_token"]))
    assert cross.status_code == 404

    # Happy delete → 204, gone from list.
    ok = await client.delete(url, headers=_auth(access))
    assert ok.status_code == 204
    listed = await client.get(f"/api/v1/projects/{project_id}/scenes", headers=_auth(access))
    assert listed.json()["data"] == []

    # Idempotent-by-404: second delete and GET after delete → 404.
    again = await client.delete(url, headers=_auth(access))
    assert again.status_code == 404
    gone = await client.get(url, headers=_auth(access))
    assert gone.status_code == 404
