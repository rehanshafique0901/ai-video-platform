"""α9.0 — Creator Analytics end-to-end against live PostgreSQL.

Proves the analytics outbox projection works across the real transaction model: the
:class:`AnalyticsProjection` consumes a publish/export lifecycle outbox event and, through the
*reused* :class:`RecordAnalyticsEvent` writer, persists exactly one owner-scoped
``analytics_events`` row — with the DB-owned ``(source_event_id, occurred_at)`` partial-unique
index guaranteeing **exactly-once** under relay redelivery (ADR-0048), and
``occurred_at = event.occurred_at`` making the dedupe coordinate deterministic. A final flow
proves the projected rows are visible through the real ``GET /api/v1/analytics/summary``.

``analytics_events`` is **append-only + immutable** (baseline ``reject_mutation`` trigger) and
its ``user_id``/``tenant_id`` FKs are ``ON DELETE SET NULL`` — so a committed row (and the
user it references) genuinely cannot be deleted on teardown (the SET NULL is itself a blocked
UPDATE). These tests therefore use a **fresh unique user per test** for isolation and
intentionally do not clean up, exactly as the immutable analytics store behaves in production.
The writer commits its own UoW (like the notifications slices), so seeds are committed too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.analytics.analytics_projection import AnalyticsProjection
from app.application.use_cases.analytics.record_analytics_event import RecordAnalyticsEvent
from app.core import container
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Seeding helpers (committed; the writer commits its own UoW)                  #
# --------------------------------------------------------------------------- #
async def _seed_user(session_factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    """Seed a committed tenant + user so the analytics FK (user_id) + tenant resolve."""
    tenant_id = uuid4()
    user_id = uuid4()
    async with session_factory() as s:
        await s.execute(insert(Tenant).values(id=tenant_id, name="AN", slug=f"an-{tenant_id}"))
        await s.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"an-{user_id}@example.com",
                display_name="AN Owner",
            )
        )
        await s.commit()
    return tenant_id, user_id


def _factory(session_factory: async_sessionmaker[AsyncSession]):
    """A fresh committing ``RecordAnalyticsEvent`` per event (own UoW), as production does."""
    return lambda: RecordAnalyticsEvent(SqlAlchemyUnitOfWork(session_factory))


def _publish_event(event_type: str, user_id: UUID, *, event_id: UUID | None = None) -> OutboxEvent:
    payload = {
        "publish_job_id": str(uuid4()),
        "project_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "social_account_id": str(uuid4()),
        "platform": "youtube",
        "source_export_job_id": str(uuid4()),
        "source_media_asset_id": str(uuid4()),
        "status": "succeeded" if event_type == "PublishJobSucceeded" else "failed",
        "version": 3,
    }
    return OutboxEvent(
        id=event_id or uuid4(),
        aggregate_type="publish_job",
        aggregate_id=uuid4(),
        event_type=event_type,
        event_version="1",
        payload=payload,
        metadata={"actor": "publish_worker"},
        occurred_at=datetime.now(UTC),
        attempts=0,
    )


def _export_event(event_type: str, user_id: UUID, *, event_id: UUID | None = None) -> OutboxEvent:
    payload = {
        "export_job_id": str(uuid4()),
        "render_job_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "format": "mp4",
        "quality": "hd_1080p",
        "orientation": "horizontal",
        "status": "succeeded" if event_type == "ExportJobSucceeded" else "failed",
        "version": 2,
    }
    return OutboxEvent(
        id=event_id or uuid4(),
        aggregate_type="export_job",
        aggregate_id=uuid4(),
        event_type=event_type,
        event_version="1",
        payload=payload,
        metadata={"actor": "export_worker"},
        occurred_at=datetime.now(UTC),
        attempts=0,
    )


async def _rows_for(session_factory: async_sessionmaker[AsyncSession], user_id: UUID) -> list[dict]:
    async with session_factory() as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT event_name, tenant_id, user_id, source_event_id, "
                        "properties, occurred_at FROM analytics_events "
                        "WHERE user_id = CAST(:u AS uuid) ORDER BY event_name"
                    ),
                    {"u": str(user_id)},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Write path: publish / export → analytics rows                               #
# --------------------------------------------------------------------------- #
async def test_publish_events_write_owner_scoped_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_user(session_factory)
    projection = AnalyticsProjection(_factory(session_factory))

    succeeded = _publish_event("PublishJobSucceeded", user_id)
    failed = _publish_event("PublishJobFailed", user_id)
    await projection(succeeded)
    await projection(failed)

    rows = await _rows_for(session_factory, user_id)
    assert [r["event_name"] for r in rows] == ["publish.failed", "publish.succeeded"]
    for r in rows:
        assert r["tenant_id"] == tenant_id  # tenant resolved from the acting user (AN5)
        assert r["user_id"] == user_id
        assert r["properties"]["source_event_type"] in {
            "PublishJobSucceeded",
            "PublishJobFailed",
        }
    by_name = {r["event_name"]: r for r in rows}
    assert by_name["publish.succeeded"]["source_event_id"] == succeeded.id
    assert by_name["publish.failed"]["source_event_id"] == failed.id


async def test_export_event_writes_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _tenant_id, user_id = await _seed_user(session_factory)
    projection = AnalyticsProjection(_factory(session_factory))

    event = _export_event("ExportJobSucceeded", user_id)
    await projection(event)

    rows = await _rows_for(session_factory, user_id)
    assert len(rows) == 1
    (row,) = rows
    assert row["event_name"] == "export.succeeded"
    assert row["source_event_id"] == event.id
    assert row["properties"]["format"] == "mp4"


async def test_redelivery_is_exactly_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _tenant_id, user_id = await _seed_user(session_factory)
    projection = AnalyticsProjection(_factory(session_factory))

    # Same event id delivered twice (the relay's at-least-once redelivery). Each delivery runs
    # in its own UoW; the DB-owned (source_event_id, occurred_at) unique index refuses the
    # second write and the projection swallows it as a no-op (ADR-0048).
    event = _publish_event("PublishJobSucceeded", user_id)
    await projection(event)
    await projection(event)

    rows = await _rows_for(session_factory, user_id)
    assert len(rows) == 1
    assert rows[0]["source_event_id"] == event.id


# --------------------------------------------------------------------------- #
# Read API (through the real /api/v1/analytics/summary endpoint)               #
# --------------------------------------------------------------------------- #
async def test_summary_aggregates_committed_owner_events(client: AsyncClient) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"an-api-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="AN API",
    )
    user_id = result.user.id
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}

    # Project a committed mix for this user (own UoW per event, as production does).
    projection = AnalyticsProjection(container.get_record_analytics_event_use_case)
    await projection(_publish_event("PublishJobSucceeded", user_id))
    await projection(_publish_event("PublishJobSucceeded", user_id))
    await projection(_publish_event("PublishJobFailed", user_id))
    await projection(_export_event("ExportJobSucceeded", user_id))

    r = await client.get("/api/v1/analytics/summary", headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    assert data["counts"]["publish.succeeded"] == 2
    assert data["counts"]["publish.failed"] == 1
    assert data["counts"]["export.succeeded"] == 1
    assert data["counts"]["export.created"] == 0
    assert data["total"] == 4
    # Full vocabulary is always present (zero-filled) and the window is echoed back.
    assert set(data["counts"]) == {
        "export.created",
        "export.succeeded",
        "export.failed",
        "publish.created",
        "publish.succeeded",
        "publish.failed",
    }
    assert "since" in data["window"] and "until" in data["window"]


async def test_fresh_user_sees_all_zero(client: AsyncClient) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"an-zero-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="AN Zero",
    )
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}

    r = await client.get("/api/v1/analytics/summary", headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == 0
    assert all(v == 0 for v in data["counts"].values())


async def test_invalid_window_is_422(client: AsyncClient) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"an-422-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="AN 422",
    )
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}
    # since after until → 422 at the schema boundary.
    r = await client.get(
        "/api/v1/analytics/summary",
        params={"since": "2026-07-02T00:00:00+00:00", "until": "2026-07-01T00:00:00+00:00"},
        headers=auth,
    )
    assert r.status_code == 422, r.text


async def test_summary_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/analytics/summary")).status_code == 401
