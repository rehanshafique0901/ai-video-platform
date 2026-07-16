"""Integration tests for ``/api/v1/media`` (α6.2).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``MediaRepository``, and the live database. Every
test uses the SAVEPOINT-rolled-back ``client`` fixture; nothing persists.

Media assets are **generation outputs** (ADR-0037): the wire DTO carries **no
``version``**, PATCH is last-writer-wins (no ``412``), and mutations never bump
``projects.version``. Endpoints are **top-level + owner-scoped** (α6.2 Q1) — no
project route gate. Register is **metadata only** (Q2); ``source`` accepts only
``uploaded`` / ``stock``. The optional links are validated in the use case — a
foreign/unknown ``project_id`` / ``scene_id`` / ``prompt_id`` / ``model_id`` is a
``422`` (``VALIDATION_FAILED``), not a ``404``. A duplicate
``(storage_backend, storage_bucket, storage_key)`` is a ``409``.

Coverage map (α6.2 pre-flight §5.3):

* A1  register happy (owner-level)     → 201 + ``MediaPublic`` (no ``version``)
* A2  register with project/scene/prompt links → 201, links echoed
* A3  register without auth            → 401
* A4  register foreign project_id      → 422
* A5  register bad kind / source=generated / bad checksum / neg size / immutable field → 422 each
* A6  register scene without project   → 422
* A7  register unknown model_id        → 422
* A8  register duplicate storage coords → 409
* A9  list empty / newest-first / filters → 200
* A10 get happy / unknown / cross-owner / non-uuid → 200/404/404/422
* A11 patch relink project / source_metadata → 200
* A12 patch same-value no-op           → 200
* A13 patch immutable field / empty    → 422 each
* A14 patch unknown model_id           → 422
* A15 delete happy / idempotent-by-404 / cross-owner → 204 then 404
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_MEDIA_KEYS = {
    "id",
    "kind",
    "source",
    "project_id",
    "scene_id",
    "prompt_id",
    "model_id",
    "provider",
    "storage_backend",
    "storage_bucket",
    "storage_key",
    "mime_type",
    "size_bytes",
    "width",
    "height",
    "duration_seconds",
    "checksum_sha256",
    "source_metadata",
    "created_at",
    "updated_at",
}

_CHECKSUM_HEX = "ab" * 32  # 64 hex chars = 32 bytes


def _fresh_email() -> str:
    return f"media-{uuid4()}@example.com"


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": _fresh_email(),
            "password": "correct horse battery staple",
            "name": "M",
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


async def _create_prompt(client: AsyncClient, access: str, project_id: str) -> str:
    r = await client.post(
        f"/api/v1/projects/{project_id}/prompts",
        headers=_auth(access),
        json={"kind": "image", "text_content": f"prompt {uuid4()}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _media_body(**over: object) -> dict:
    body: dict = {
        "kind": "image",
        "source": "uploaded",
        "storage_backend": "s3",
        "storage_bucket": "assets",
        "storage_key": f"uploads/{uuid4()}.png",
        "mime_type": "image/png",
        "size_bytes": 2048,
        "checksum_sha256": _CHECKSUM_HEX,
    }
    body.update(over)
    return body


async def _register_media(client: AsyncClient, access: str, **over: object) -> dict:
    r = await client.post("/api/v1/media", headers=_auth(access), json=_media_body(**over))
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---- A1 — register happy (owner-level) --------------------------------


@pytest.mark.integration
async def test_a1_register_happy_owner_level(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.post("/api/v1/media", headers=_auth(access), json=_media_body(source="stock"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]
    data = body["data"]
    assert set(data.keys()) == _MEDIA_KEYS
    assert data["kind"] == "image"
    assert data["source"] == "stock"
    assert data["project_id"] is None
    assert data["scene_id"] is None
    assert data["prompt_id"] is None
    assert data["model_id"] is None
    assert data["size_bytes"] == 2048
    # checksum echoed as lowercase hex.
    assert data["checksum_sha256"] == _CHECKSUM_HEX
    assert data["source_metadata"] == {}
    # Generation output (ADR-0037): no OCC token, no server-internal leaks.
    assert "version" not in data
    assert "owner_user_id" not in data
    assert "tenant_id" not in data
    assert "deleted_at" not in data


# ---- A2 — register with links -----------------------------------------


@pytest.mark.integration
async def test_a2_register_with_links(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene_id = await _create_scene(client, access, project_id)
    prompt_id = await _create_prompt(client, access, project_id)

    data = await _register_media(
        client,
        access,
        kind="video",
        project_id=project_id,
        scene_id=scene_id,
        prompt_id=prompt_id,
        width=1920,
        height=1080,
        duration_seconds=12.5,
        source_metadata={"origin": "unsplash"},
    )
    assert data["project_id"] == project_id
    assert data["scene_id"] == scene_id
    assert data["prompt_id"] == prompt_id
    assert data["width"] == 1920
    assert data["duration_seconds"] == 12.5
    assert data["source_metadata"] == {"origin": "unsplash"}


# ---- A3 — register without auth ---------------------------------------


@pytest.mark.integration
async def test_a3_register_without_auth_401(client: AsyncClient) -> None:
    r = await client.post("/api/v1/media", json=_media_body())
    assert r.status_code == 401, r.text


# ---- A4 — register foreign project_id ---------------------------------


@pytest.mark.integration
async def test_a4_register_foreign_project_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.post(
        "/api/v1/media",
        headers=_auth(access),
        json=_media_body(project_id=str(uuid4())),  # not the caller's project
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- A5 — register bad body variants ----------------------------------


@pytest.mark.integration
async def test_a5_register_bad_body_variants_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    # bad kind
    r = await client.post("/api/v1/media", headers=_auth(access), json=_media_body(kind="bogus"))
    assert r.status_code == 422, r.text
    # source=generated is not register-allowed (α6.2 Q2)
    r = await client.post(
        "/api/v1/media", headers=_auth(access), json=_media_body(source="generated")
    )
    assert r.status_code == 422, r.text
    # malformed checksum (not 64 hex)
    r = await client.post(
        "/api/v1/media", headers=_auth(access), json=_media_body(checksum_sha256="deadbeef")
    )
    assert r.status_code == 422, r.text
    # negative size
    r = await client.post("/api/v1/media", headers=_auth(access), json=_media_body(size_bytes=-1))
    assert r.status_code == 422, r.text
    # unknown/immutable extra field rejected (extra="forbid")
    r = await client.post(
        "/api/v1/media", headers=_auth(access), json=_media_body(owner_user_id=str(uuid4()))
    )
    assert r.status_code == 422, r.text


# ---- A6 — register scene without project ------------------------------


@pytest.mark.integration
async def test_a6_register_scene_without_project_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene_id = await _create_scene(client, access, project_id)

    r = await client.post(
        "/api/v1/media",
        headers=_auth(access),
        json=_media_body(scene_id=scene_id),  # scene without project
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- A7 — register unknown model_id -----------------------------------


@pytest.mark.integration
async def test_a7_register_unknown_model_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.post(
        "/api/v1/media", headers=_auth(access), json=_media_body(model_id=str(uuid4()))
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- A8 — register duplicate storage coords ---------------------------


@pytest.mark.integration
async def test_a8_register_duplicate_coords_409(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    # Unique-per-run key (reused within the test) — the storage-coords unique
    # constraint is global, so a hardcoded key is fragile to any residual row
    # committed by an earlier interrupted run.
    coords = {
        "storage_backend": "s3",
        "storage_bucket": "assets",
        "storage_key": f"dup/{uuid4()}.png",
    }

    await _register_media(client, access, **coords)
    r = await client.post("/api/v1/media", headers=_auth(access), json=_media_body(**coords))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "CONFLICT"


# ---- A9 — list empty / newest-first / filters -------------------------


@pytest.mark.integration
async def test_a9_list_empty_then_newest_first_and_filters(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.get("/api/v1/media", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []

    first = await _register_media(client, access, kind="image")
    second = await _register_media(client, access, kind="video", source="stock")

    r = await client.get("/api/v1/media", headers=_auth(access))
    rows = r.json()["data"]
    # Both assets come back.
    assert {m["id"] for m in rows} == {first["id"], second["id"]}
    # ``created_at`` defaults to ``now()``, which Postgres holds CONSTANT for the
    # whole (single) test transaction, so the two rows tie on time and the
    # endpoint's ``created_at DESC, id DESC`` order reduces to the deterministic
    # ``id DESC`` tiebreak. Assert the endpoint applied that total order (i.e. it
    # sorts rather than returning insertion/arbitrary order) instead of relying on
    # a nondeterministic ``uuid4`` comparison against insertion order.
    returned_ids = [m["id"] for m in rows]
    expected_ids = [
        m["id"] for m in sorted(rows, key=lambda m: (m["created_at"], m["id"]), reverse=True)
    ]
    assert returned_ids == expected_ids

    r = await client.get("/api/v1/media?kind=video", headers=_auth(access))
    assert [m["id"] for m in r.json()["data"]] == [second["id"]]
    r = await client.get("/api/v1/media?source=stock", headers=_auth(access))
    assert [m["id"] for m in r.json()["data"]] == [second["id"]]


# ---- A10 — get happy / unknown / cross-owner / non-uuid ---------------


@pytest.mark.integration
async def test_a10_get_variants(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    media = await _register_media(client, access)

    r = await client.get(f"/api/v1/media/{media['id']}", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["id"] == media["id"]

    r = await client.get(f"/api/v1/media/{uuid4()}", headers=_auth(access))
    assert r.status_code == 404, r.text

    other = await _register(client)
    r = await client.get(f"/api/v1/media/{media['id']}", headers=_auth(other["access_token"]))
    assert r.status_code == 404, r.text  # cross-owner → indistinguishable 404

    r = await client.get("/api/v1/media/not-a-uuid", headers=_auth(access))
    assert r.status_code == 422, r.text


# ---- A11 — patch relink project / source_metadata ---------------------


@pytest.mark.integration
async def test_a11_patch_relink_and_metadata(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    media = await _register_media(client, access)

    r = await client.patch(
        f"/api/v1/media/{media['id']}",
        headers=_auth(access),
        json={"project_id": project_id, "source_metadata": {"note": "linked"}},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["project_id"] == project_id
    assert data["source_metadata"] == {"note": "linked"}
    assert "version" not in data


# ---- A12 — patch same-value no-op -------------------------------------


@pytest.mark.integration
async def test_a12_patch_same_value_noop(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    media = await _register_media(client, access, provider="runway")

    r = await client.patch(
        f"/api/v1/media/{media['id']}",
        headers=_auth(access),
        json={"provider": "runway"},  # already this value
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["provider"] == "runway"


# ---- A13 — patch immutable field / empty ------------------------------


@pytest.mark.integration
async def test_a13_patch_immutable_or_empty_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    media = await _register_media(client, access)

    # storage_key is immutable → extra="forbid" rejects it.
    r = await client.patch(
        f"/api/v1/media/{media['id']}",
        headers=_auth(access),
        json={"storage_key": "new/key.png"},
    )
    assert r.status_code == 422, r.text
    # empty patch → 422
    r = await client.patch(f"/api/v1/media/{media['id']}", headers=_auth(access), json={})
    assert r.status_code == 422, r.text


# ---- A14 — patch unknown model_id -------------------------------------


@pytest.mark.integration
async def test_a14_patch_unknown_model_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    media = await _register_media(client, access)

    r = await client.patch(
        f"/api/v1/media/{media['id']}",
        headers=_auth(access),
        json={"model_id": str(uuid4())},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- A15 — delete happy / idempotent-by-404 / cross-owner -------------


@pytest.mark.integration
async def test_a15_delete_then_idempotent_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    media = await _register_media(client, access)

    r = await client.delete(f"/api/v1/media/{media['id']}", headers=_auth(access))
    assert r.status_code == 204, r.text
    # second delete → 404 (idempotent-by-404)
    r = await client.delete(f"/api/v1/media/{media['id']}", headers=_auth(access))
    assert r.status_code == 404, r.text
    # GET after delete → 404
    r = await client.get(f"/api/v1/media/{media['id']}", headers=_auth(access))
    assert r.status_code == 404, r.text


@pytest.mark.integration
async def test_a15b_delete_cross_owner_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    media = await _register_media(client, access)

    other = await _register(client)
    r = await client.delete(f"/api/v1/media/{media['id']}", headers=_auth(other["access_token"]))
    assert r.status_code == 404, r.text
    # still alive for the real owner
    r = await client.get(f"/api/v1/media/{media['id']}", headers=_auth(access))
    assert r.status_code == 200, r.text
