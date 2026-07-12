"""Integration tests for ``/api/v1/projects/{project_id}/versions`` (α5d.1).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``ProjectVersionRepository`` (+ snapshot
assembly from the live project/storyboard/scenes), and the database. Every
test uses the SAVEPOINT-rolled-back ``client`` fixture; nothing persists.

Coverage map (α5d pre-flight §7 / §8):

* H1  create happy                 → 201 + ``ProjectVersionDetail`` (v#=1, manual_save)
* H2  create without auth          → 401
* H3  create on unowned/unknown project → 404
* H4  create with extra body field → 422 (``extra="forbid"``)
* H5  create captures scenes       → snapshot scenes match created ids/order
* H6  second create links parent   → v#=2, parent_version_id == first id
* H7  list empty                   → 200, ``data == []``
* H8  list newest-first metadata   → 200, v# [2,1], no snapshot in items
* H9  list unowned project         → 404
* H10 get happy                    → 200, ``ProjectVersionDetail`` with snapshot
* H11 get unknown version          → 404
* H12 get version of another project → 404 (cross-project isolation)
* H13 get non-UUID version path    → 422
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_PUBLIC_KEYS = {
    "id",
    "project_id",
    "version_number",
    "reason",
    "parent_version_id",
    "created_by_user_id",
    "created_at",
    "is_current",
}
_DETAIL_KEYS = _PUBLIC_KEYS | {"snapshot", "diff_summary"}


def _fresh_email() -> str:
    return f"version-{uuid4()}@example.com"


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": _fresh_email(),
            "password": "correct horse battery staple",
            "name": "V",
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


async def _create_scene(client: AsyncClient, access: str, project_id: str) -> str:
    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes",
        headers=_auth(access),
        json={"title": f"Scene {uuid4()}", "duration_seconds": 5.0},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


async def _create_version(client: AsyncClient, access: str, project_id: str) -> dict:
    r = await client.post(
        f"/api/v1/projects/{project_id}/versions",
        headers=_auth(access),
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---- H1 — create happy -------------------------------------------------


@pytest.mark.integration
async def test_h1_create_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    r = await client.post(f"/api/v1/projects/{project_id}/versions", headers=_auth(access))
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]
    data = body["data"]
    assert set(data.keys()) == _DETAIL_KEYS
    assert data["version_number"] == 1
    assert data["reason"] == "manual_save"
    assert data["parent_version_id"] is None
    assert data["diff_summary"] is None
    assert data["project_id"] == project_id
    assert data["is_current"] is True
    assert data["snapshot"]["schema_version"] == 1
    assert data["snapshot"]["scenes"] == []


# ---- H2 — create without auth -----------------------------------------


@pytest.mark.integration
async def test_h2_create_without_auth_401(client: AsyncClient) -> None:
    r = await client.post(f"/api/v1/projects/{uuid4()}/versions")
    assert r.status_code == 401, r.text


# ---- H3 — create on unowned/unknown project ---------------------------


@pytest.mark.integration
async def test_h3_create_unowned_project_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    r = await client.post(f"/api/v1/projects/{uuid4()}/versions", headers=_auth(access))
    assert r.status_code == 404, r.text


# ---- H4 — create with extra body field --------------------------------


@pytest.mark.integration
async def test_h4_create_extra_field_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.post(
        f"/api/v1/projects/{project_id}/versions",
        headers=_auth(access),
        json={"reason": "autosave"},  # server-owned — not client-supplied
    )
    assert r.status_code == 422, r.text


# ---- H5 — create captures scenes --------------------------------------


@pytest.mark.integration
async def test_h5_create_captures_scenes(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene_a = await _create_scene(client, access, project_id)
    scene_b = await _create_scene(client, access, project_id)

    data = await _create_version(client, access, project_id)
    snap_scene_ids = [s["id"] for s in data["snapshot"]["scenes"]]
    assert snap_scene_ids == [scene_a, scene_b]
    assert data["snapshot"]["storyboard"] is not None
    # Numeric duration serialized as a lossless string (α5d Q7).
    assert data["snapshot"]["scenes"][0]["duration_seconds"] == "5.000"


# ---- H6 — second create links parent ----------------------------------


@pytest.mark.integration
async def test_h6_second_create_links_parent(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    first = await _create_version(client, access, project_id)
    second = await _create_version(client, access, project_id)
    assert second["version_number"] == 2
    assert second["parent_version_id"] == first["id"]


# ---- H7 — list empty ---------------------------------------------------


@pytest.mark.integration
async def test_h7_list_empty(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    r = await client.get(f"/api/v1/projects/{project_id}/versions", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


# ---- H8 — list newest-first metadata ----------------------------------


@pytest.mark.integration
async def test_h8_list_newest_first_metadata(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    await _create_version(client, access, project_id)
    await _create_version(client, access, project_id)

    r = await client.get(f"/api/v1/projects/{project_id}/versions", headers=_auth(access))
    assert r.status_code == 200, r.text
    items = r.json()["data"]
    assert [i["version_number"] for i in items] == [2, 1]
    # LIST is metadata-only — no snapshot bodies (α5d Q4).
    assert set(items[0].keys()) == _PUBLIC_KEYS
    # Newest capture is current; the prior version is not (α5d pre-flight §4).
    assert items[0]["is_current"] is True
    assert items[1]["is_current"] is False


# ---- H9 — list unowned project ----------------------------------------


@pytest.mark.integration
async def test_h9_list_unowned_project_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    r = await client.get(f"/api/v1/projects/{uuid4()}/versions", headers=_auth(access))
    assert r.status_code == 404, r.text


# ---- H10 — get happy ---------------------------------------------------


@pytest.mark.integration
async def test_h10_get_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    created = await _create_version(client, access, project_id)

    r = await client.get(
        f"/api/v1/projects/{project_id}/versions/{created['id']}",
        headers=_auth(access),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert set(data.keys()) == _DETAIL_KEYS
    assert data["id"] == created["id"]
    assert data["is_current"] is True
    assert data["snapshot"]["schema_version"] == 1


# ---- H11 — get unknown version ----------------------------------------


@pytest.mark.integration
async def test_h11_get_unknown_version_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.get(f"/api/v1/projects/{project_id}/versions/{uuid4()}", headers=_auth(access))
    assert r.status_code == 404, r.text


# ---- H12 — get version of another project -----------------------------


@pytest.mark.integration
async def test_h12_get_version_of_another_project_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_a = await _create_project(client, access)
    project_b = await _create_project(client, access)
    version_a = await _create_version(client, access, project_a)

    # The version belongs to project A; addressing it via project B → 404.
    r = await client.get(
        f"/api/v1/projects/{project_b}/versions/{version_a['id']}",
        headers=_auth(access),
    )
    assert r.status_code == 404, r.text


# ---- H13 — get non-UUID version path ----------------------------------


@pytest.mark.integration
async def test_h13_get_non_uuid_version_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.get(
        f"/api/v1/projects/{project_id}/versions/not-a-uuid", headers=_auth(access)
    )
    assert r.status_code == 422, r.text
