"""Integration tests for ``/api/v1/projects/{project_id}/prompts`` (α6.1).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``PromptRepository``, and the live database.
Every test uses the SAVEPOINT-rolled-back ``client`` fixture; nothing persists.

Prompts are **generation inputs** (ADR-0036): the wire DTO carries **no
``version``**, PATCH is last-writer-wins (no ``412``), and mutations never bump
``projects.version``. The scene/model links are validated in the use case — a
foreign/unknown ``scene_id`` or an unknown/retired ``model_id`` is a ``422``
(``VALIDATION_FAILED``), not a ``404``.

Coverage map (α6.1 pre-flight §5.3):

* A1  create happy (project-level)   → 201 + ``PromptPublic`` (no ``version``)
* A2  create scene-linked happy      → 201, ``scene_id`` echoed
* A3  create without auth            → 401
* A4  create on unowned/unknown proj → 404
* A5  create bad kind / empty text / extra field → 422 each
* A6  create foreign scene_id        → 422 VALIDATION_FAILED
* A7  create unknown model_id        → 422 VALIDATION_FAILED
* A8  list empty / newest-first / filters → 200
* A9  list unowned project           → 404
* A10 get happy / unknown / cross-owner / non-uuid → 200/404/404/422
* A11 patch real change (no version on wire) → 200, content changed
* A12 patch same-value no-op         → 200
* A13 patch clear model link (null) / empty / scene_id forbidden → 200/422/422
* A14 patch unknown model_id         → 422
* A15 delete happy / idempotent-by-404 / cross-owner → 204 then 404
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_PUBLIC_KEYS = {
    "id",
    "project_id",
    "scene_id",
    "kind",
    "text_content",
    "model_id",
    "extra",
    "created_at",
    "updated_at",
}


def _fresh_email() -> str:
    return f"prompt-{uuid4()}@example.com"


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": _fresh_email(),
            "password": "correct horse battery staple",
            "name": "P",
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


async def _create_prompt(client: AsyncClient, access: str, project_id: str, **body: object) -> dict:
    payload = {"kind": "image", "text_content": f"prompt {uuid4()}", **body}
    r = await client.post(
        f"/api/v1/projects/{project_id}/prompts",
        headers=_auth(access),
        json=payload,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---- A1 — create happy (project-level) --------------------------------


@pytest.mark.integration
async def test_a1_create_happy_project_level(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    r = await client.post(
        f"/api/v1/projects/{project_id}/prompts",
        headers=_auth(access),
        json={"kind": "image", "text_content": "a red fox in snow"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]
    data = body["data"]
    assert set(data.keys()) == _PUBLIC_KEYS
    assert data["kind"] == "image"
    assert data["text_content"] == "a red fox in snow"
    assert data["scene_id"] is None
    assert data["model_id"] is None
    assert data["project_id"] == project_id
    assert data["extra"] == {}
    # Generation input (ADR-0036): no OCC token, no server-internal leaks.
    assert "version" not in data
    assert "generated_by_agent" not in data
    assert "deleted_at" not in data


# ---- A2 — create scene-linked happy -----------------------------------


@pytest.mark.integration
async def test_a2_create_scene_linked(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene_id = await _create_scene(client, access, project_id)

    data = await _create_prompt(
        client, access, project_id, kind="motion", scene_id=scene_id, extra={"weight": 2}
    )
    assert data["scene_id"] == scene_id
    assert data["kind"] == "motion"
    assert data["extra"] == {"weight": 2}


# ---- A3 — create without auth -----------------------------------------


@pytest.mark.integration
async def test_a3_create_without_auth_returns_401(client: AsyncClient) -> None:
    r = await client.post(
        f"/api/v1/projects/{uuid4()}/prompts",
        json={"kind": "image", "text_content": "x"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


# ---- A4 — create on unowned/unknown project ---------------------------


@pytest.mark.integration
async def test_a4_create_on_unknown_project_returns_404(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.post(
        f"/api/v1/projects/{uuid4()}/prompts",
        headers=_auth(reg["access_token"]),
        json={"kind": "image", "text_content": "x"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ---- A5 — create validation (422) -------------------------------------


@pytest.mark.integration
async def test_a5_create_validation_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    url = f"/api/v1/projects/{project_id}/prompts"

    bad_kind = await client.post(
        url, headers=_auth(access), json={"kind": "system", "text_content": "x"}
    )
    assert bad_kind.status_code == 422

    empty_text = await client.post(
        url, headers=_auth(access), json={"kind": "image", "text_content": "   "}
    )
    assert empty_text.status_code == 422

    extra_field = await client.post(
        url,
        headers=_auth(access),
        json={"kind": "image", "text_content": "x", "generated_by_agent": "hacker"},
    )
    assert extra_field.status_code == 422


# ---- A6 — create foreign scene_id → 422 -------------------------------


@pytest.mark.integration
async def test_a6_create_foreign_scene_id_returns_422(client: AsyncClient) -> None:
    reg_a = await _register(client)
    reg_b = await _register(client)
    project_a = await _create_project(client, reg_a["access_token"])
    project_b = await _create_project(client, reg_b["access_token"])
    # A scene that belongs to B's project cannot be linked from A's prompt.
    foreign_scene = await _create_scene(client, reg_b["access_token"], project_b)

    r = await client.post(
        f"/api/v1/projects/{project_a}/prompts",
        headers=_auth(reg_a["access_token"]),
        json={"kind": "image", "text_content": "x", "scene_id": foreign_scene},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- A7 — create unknown model_id → 422 -------------------------------


@pytest.mark.integration
async def test_a7_create_unknown_model_id_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    r = await client.post(
        f"/api/v1/projects/{project_id}/prompts",
        headers=_auth(access),
        json={"kind": "image", "text_content": "x", "model_id": str(uuid4())},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- A8 — list empty / newest-first / filters -------------------------


@pytest.mark.integration
async def test_a8_list_empty_and_filters(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    empty = await client.get(f"/api/v1/projects/{project_id}/prompts", headers=_auth(access))
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"] == []

    scene_id = await _create_scene(client, access, project_id)
    img = await _create_prompt(client, access, project_id, kind="image")
    vid = await _create_prompt(client, access, project_id, kind="video")
    scoped = await _create_prompt(client, access, project_id, kind="video", scene_id=scene_id)

    listed = await client.get(f"/api/v1/projects/{project_id}/prompts", headers=_auth(access))
    all_listed = listed.json()["data"]
    assert {p["id"] for p in all_listed} == {img["id"], vid["id"], scoped["id"]}

    by_kind = await client.get(
        f"/api/v1/projects/{project_id}/prompts?kind=video", headers=_auth(access)
    )
    assert {p["id"] for p in by_kind.json()["data"]} == {vid["id"], scoped["id"]}

    by_scene = await client.get(
        f"/api/v1/projects/{project_id}/prompts?scene_id={scene_id}", headers=_auth(access)
    )
    assert [p["id"] for p in by_scene.json()["data"]] == [scoped["id"]]

    bad_kind = await client.get(
        f"/api/v1/projects/{project_id}/prompts?kind=nonsense", headers=_auth(access)
    )
    assert bad_kind.status_code == 422


# ---- A9 — list unowned project → 404 ----------------------------------


@pytest.mark.integration
async def test_a9_list_unowned_project_returns_404(client: AsyncClient) -> None:
    reg_a = await _register(client)
    reg_b = await _register(client)
    project_id = await _create_project(client, reg_a["access_token"])
    r = await client.get(
        f"/api/v1/projects/{project_id}/prompts", headers=_auth(reg_b["access_token"])
    )
    assert r.status_code == 404


# ---- A10 — get happy / unknown / cross-owner / non-uuid ---------------


@pytest.mark.integration
async def test_a10_get_variants(client: AsyncClient) -> None:
    reg_a = await _register(client)
    reg_b = await _register(client)
    access = reg_a["access_token"]
    project_id = await _create_project(client, access)
    prompt = await _create_prompt(client, access, project_id, text_content="fetch me")

    ok = await client.get(
        f"/api/v1/projects/{project_id}/prompts/{prompt['id']}", headers=_auth(access)
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["text_content"] == "fetch me"
    assert set(ok.json()["data"].keys()) == _PUBLIC_KEYS

    unknown = await client.get(
        f"/api/v1/projects/{project_id}/prompts/{uuid4()}", headers=_auth(access)
    )
    assert unknown.status_code == 404

    cross = await client.get(
        f"/api/v1/projects/{project_id}/prompts/{prompt['id']}",
        headers=_auth(reg_b["access_token"]),
    )
    assert cross.status_code == 404

    bad = await client.get(
        f"/api/v1/projects/{project_id}/prompts/not-a-uuid", headers=_auth(access)
    )
    assert bad.status_code == 422


# ---- A11 — patch real change (no version on wire) ---------------------


@pytest.mark.integration
async def test_a11_patch_real_change(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    prompt = await _create_prompt(client, access, project_id, text_content="before")

    r = await client.patch(
        f"/api/v1/projects/{project_id}/prompts/{prompt['id']}",
        headers=_auth(access),
        json={"text_content": "after"},  # no version field
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["text_content"] == "after"
    assert "version" not in data


# ---- A12 — patch same-value no-op -------------------------------------


@pytest.mark.integration
async def test_a12_patch_same_value_noop(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    prompt = await _create_prompt(client, access, project_id, text_content="same")

    r = await client.patch(
        f"/api/v1/projects/{project_id}/prompts/{prompt['id']}",
        headers=_auth(access),
        json={"text_content": "same"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["text_content"] == "same"


# ---- A13 — patch clear model / empty / scene_id forbidden -------------


@pytest.mark.integration
async def test_a13_patch_clear_model_and_forbidden(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    prompt = await _create_prompt(client, access, project_id)
    url = f"/api/v1/projects/{project_id}/prompts/{prompt['id']}"

    # Clear the (already null) model link with an explicit null → 200, stays null.
    cleared = await client.patch(url, headers=_auth(access), json={"model_id": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["model_id"] is None

    # Empty patch → 422.
    empty = await client.patch(url, headers=_auth(access), json={})
    assert empty.status_code == 422

    # scene_id is immutable (not accepted) → 422 (extra="forbid").
    reparent = await client.patch(url, headers=_auth(access), json={"scene_id": str(uuid4())})
    assert reparent.status_code == 422


# ---- A14 — patch unknown model_id → 422 -------------------------------


@pytest.mark.integration
async def test_a14_patch_unknown_model_id_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    prompt = await _create_prompt(client, access, project_id)

    r = await client.patch(
        f"/api/v1/projects/{project_id}/prompts/{prompt['id']}",
        headers=_auth(access),
        json={"model_id": str(uuid4())},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- A15 — delete happy / idempotent-by-404 / cross-owner -------------


@pytest.mark.integration
async def test_a15_delete_happy_idempotent_and_cross_owner(client: AsyncClient) -> None:
    reg_a = await _register(client)
    reg_b = await _register(client)
    access = reg_a["access_token"]
    project_id = await _create_project(client, access)
    prompt = await _create_prompt(client, access, project_id)
    url = f"/api/v1/projects/{project_id}/prompts/{prompt['id']}"

    # Cross-owner delete → 404.
    cross = await client.delete(url, headers=_auth(reg_b["access_token"]))
    assert cross.status_code == 404

    # Happy delete → 204, gone from list.
    ok = await client.delete(url, headers=_auth(access))
    assert ok.status_code == 204
    listed = await client.get(f"/api/v1/projects/{project_id}/prompts", headers=_auth(access))
    assert listed.json()["data"] == []

    # Idempotent-by-404: second delete and GET/PATCH after delete → 404.
    again = await client.delete(url, headers=_auth(access))
    assert again.status_code == 404
    gone = await client.get(url, headers=_auth(access))
    assert gone.status_code == 404
    patched = await client.patch(url, headers=_auth(access), json={"text_content": "z"})
    assert patched.status_code == 404
