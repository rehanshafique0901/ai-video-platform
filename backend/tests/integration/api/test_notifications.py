"""Integration tests for ``/api/v1/notifications`` (α8.5b.3r read API).

End-to-end coverage through middleware, exception handlers, DI, ``get_current_user``, the
real ``NotificationRepository``, and the live database. Every test uses the
SAVEPOINT-rolled-back ``client`` fixture; nothing persists.

Notifications are **projected from events**, not created via HTTP (α8.5b.3), so there is no
create endpoint to seed against in-process. These tests therefore verify the read-API
**wiring, auth, envelope, pagination shape, and the uniform 404 path** end-to-end; the
seeded-data behaviour (ordering, counts, mark-read/all semantics, owner isolation) is
proven by the repository integration suite (``test_notification_repository.py`` N5–N8) and
the use-case unit suite (``test_notification_read_api.py``). This is the platform's layered
proof cadence (α8.5b.3 idempotency precedent).

Coverage:

* T1  list without auth / unread-count / mark-read / read-all      → 401 each
* T2  list empty                                                    → 200 [] + meta, no next_cursor
* T3  unread-count for a fresh user                                 → 200 { count: 0 }
* T4  read-all for a fresh user                                     → 200 { updated: 0 }
* T5  mark-read unknown id                                          → 404
* T6  mark-read non-uuid path                                       → 422
* T7  list bad cursor                                               → 422
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration

_NOTIFICATION_KEYS = {
    "id",
    "user_id",
    "kind",
    "title",
    "body",
    "payload",
    "source_event_id",
    "read_at",
    "created_at",
    "updated_at",
}


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> str:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"notif-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "N",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["access_token"]


async def test_t1_endpoints_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/notifications")).status_code == 401
    assert (await client.get("/api/v1/notifications/unread-count")).status_code == 401
    assert (await client.post(f"/api/v1/notifications/{uuid4()}/read")).status_code == 401
    assert (await client.post("/api/v1/notifications/read-all")).status_code == 401


async def test_t2_list_empty(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.get("/api/v1/notifications", headers=_auth(access))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == []
    assert "next_cursor" not in body["meta"]  # last page by omission


async def test_t3_unread_count_zero(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.get("/api/v1/notifications/unread-count", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"count": 0}


async def test_t4_read_all_zero(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post("/api/v1/notifications/read-all", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"updated": 0}


async def test_t5_mark_read_unknown_is_404(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post(f"/api/v1/notifications/{uuid4()}/read", headers=_auth(access))
    assert r.status_code == 404, r.text


async def test_t6_mark_read_non_uuid_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.post("/api/v1/notifications/not-a-uuid/read", headers=_auth(access))
    assert r.status_code == 422, r.text


async def test_t7_list_bad_cursor_is_422(client: AsyncClient) -> None:
    access = await _register(client)
    r = await client.get(
        "/api/v1/notifications", headers=_auth(access), params={"cursor": "!!bad!!"}
    )
    assert r.status_code == 422, r.text
