"""α8.9a — Publish Notifications end-to-end against live PostgreSQL (deferred DQ7).

Proves the publish notification fan-out works across the real transaction model: the
:class:`PublishNotificationProjection` consumes a ``PublishJobSucceeded`` /
``PublishJobFailed`` outbox event and, through the *reused* :class:`CreateNotification`
writer, persists exactly one owner-scoped ``notifications`` row — with the DB-owned
``(user_id, source_event_id)`` partial-unique index guaranteeing exactly-once under relay
redelivery. A final flow proves the projected notification is visible through the real
``/api/v1/notifications`` read API.

Like the generation / asset-promotion slices, the ``CreateNotification`` use case *commits*
(its UoW owns its session), so these tests seed committed rows and clean them up on teardown
rather than leaning on the SAVEPOINT ``session`` fixture. Nothing new is added to the
notification subsystem — the repository, the writer, the uniqueness invariant, and the read
API are all reused unchanged (α8.5b.3 / α8.5b.3r).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.publisher import OutboxEvent
from app.application.use_cases.notifications.create_notification import CreateNotification
from app.application.use_cases.notifications.publish_notification_projection import (
    PublishNotificationProjection,
)
from app.core import container
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Seeding / cleanup helpers (committed; the writer commits its own UoW)        #
# --------------------------------------------------------------------------- #
async def _seed_user(session_factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    """Seed a committed tenant + user so the notification FK (user_id) resolves."""
    tenant_id = uuid4()
    user_id = uuid4()
    async with session_factory() as s:
        await s.execute(insert(Tenant).values(id=tenant_id, name="PN", slug=f"pn-{tenant_id}"))
        await s.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"pn-{user_id}@example.com",
                display_name="PN Owner",
            )
        )
        await s.commit()
    return tenant_id, user_id


async def _cleanup_user(
    session_factory: async_sessionmaker[AsyncSession], *, tenant_id: UUID, user_id: UUID
) -> None:
    async with session_factory() as s:
        # Deleting the user cascades sessions / roles_users / notifications (FK CASCADE);
        # the tenant is ON DELETE RESTRICT so it must go last.
        await s.execute(text("DELETE FROM users WHERE id = CAST(:u AS uuid)"), {"u": str(user_id)})
        await s.execute(
            text("DELETE FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": str(tenant_id)}
        )
        await s.commit()


def _factory(session_factory: async_sessionmaker[AsyncSession]):
    """A fresh committing ``CreateNotification`` per event (own UoW), as production does."""
    return lambda: CreateNotification(SqlAlchemyUnitOfWork(session_factory))


def _event(event_type: str, user_id: UUID, *, event_id: UUID | None = None) -> OutboxEvent:
    base = {
        "publish_job_id": str(uuid4()),
        "project_id": str(uuid4()),
        "requested_by_user_id": str(user_id),
        "social_account_id": str(uuid4()),
        "platform": "youtube",
        "source_export_job_id": str(uuid4()),
        "source_media_asset_id": str(uuid4()),
        "version": 3,
    }
    if event_type == "PublishJobSucceeded":
        base.update(
            {
                "status": "succeeded",
                "platform_post_id": "yt-abc123",
                "platform_post_url": "https://youtube.com/watch?v=abc123",
                "published_at": "2026-07-27T10:30:00+00:00",
            }
        )
    else:
        base.update(
            {
                "status": "failed",
                "error": {"code": "upload_rejected", "message": "The destination rejected it."},
            }
        )
    return OutboxEvent(
        id=event_id or uuid4(),
        aggregate_type="publish_job",
        aggregate_id=uuid4(),
        event_type=event_type,
        event_version="1",
        payload=base,
        metadata={"actor": "publish_worker"},
        occurred_at=datetime.now(UTC),
        attempts=0,
    )


async def _notifications_for(
    session_factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> list[dict]:
    async with session_factory() as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT id, kind, title, body, payload, source_event_id, "
                        "delivered_in_app_at, read_at FROM notifications "
                        "WHERE user_id = CAST(:u AS uuid) ORDER BY created_at"
                    ),
                    {"u": str(user_id)},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Success / failure / exactly-once                                            #
# --------------------------------------------------------------------------- #
async def test_success_event_projects_publish_succeeded_notification(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_user(session_factory)
    try:
        projection = PublishNotificationProjection(_factory(session_factory))
        event = _event("PublishJobSucceeded", user_id)

        await projection(event)

        rows = await _notifications_for(session_factory, user_id)
        assert len(rows) == 1
        (row,) = rows
        assert row["kind"] == "publish.succeeded"
        assert row["title"] == "Your video was published"
        assert "youtube" in (row["body"] or "")
        assert row["source_event_id"] == event.id
        assert row["delivered_in_app_at"] is not None  # in-app delivery = committed row
        assert row["read_at"] is None
        assert row["payload"]["publish_job_id"] == event.payload["publish_job_id"]
        assert row["payload"]["platform_post_url"] == event.payload["platform_post_url"]
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)


async def test_failure_event_projects_publish_failed_notification(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_user(session_factory)
    try:
        projection = PublishNotificationProjection(_factory(session_factory))
        event = _event("PublishJobFailed", user_id)

        await projection(event)

        rows = await _notifications_for(session_factory, user_id)
        assert len(rows) == 1
        (row,) = rows
        assert row["kind"] == "publish.failed"
        assert row["title"] == "Your video couldn't be published"
        assert row["body"] == "The destination rejected it."
        assert row["source_event_id"] == event.id
        assert row["payload"]["error"]["code"] == "upload_rejected"
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)


async def test_redelivery_is_exactly_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_user(session_factory)
    try:
        projection = PublishNotificationProjection(_factory(session_factory))
        # Same event id delivered twice (the relay's at-least-once redelivery). Each
        # delivery runs in its own UoW; the DB-owned (user_id, source_event_id) unique
        # index refuses the second write and the projection swallows it as a no-op.
        event = _event("PublishJobSucceeded", user_id)
        await projection(event)
        await projection(event)

        rows = await _notifications_for(session_factory, user_id)
        assert len(rows) == 1
        assert rows[0]["source_event_id"] == event.id
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)


# --------------------------------------------------------------------------- #
# Read-API visibility (through the real /api/v1/notifications endpoints)       #
# --------------------------------------------------------------------------- #
async def test_projected_notification_visible_via_read_api(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # A committed auth context (user + session + token), so the read API can authenticate
    # the recipient and READ COMMITTED lets it see the projected, committed notification.
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"pn-api-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="PN API",
    )
    user_id = result.user.id
    tenant_id = result.tenant.id
    access = result.tokens.access_token
    auth = {"Authorization": f"Bearer {access}"}
    try:
        # Project a publish success notification for this user (committed, own UoW).
        projection = PublishNotificationProjection(container.get_create_notification_use_case)
        await projection(_event("PublishJobSucceeded", user_id))

        # The unread badge reflects it.
        rc = await client.get("/api/v1/notifications/unread-count", headers=auth)
        assert rc.status_code == 200, rc.text
        assert rc.json()["data"]["count"] >= 1

        # The feed returns it, projected onto the public DTO.
        rl = await client.get("/api/v1/notifications", headers=auth)
        assert rl.status_code == 200, rl.text
        items = rl.json()["data"]
        published = [n for n in items if n["kind"] == "publish.succeeded"]
        assert len(published) == 1
        assert published[0]["title"] == "Your video was published"
        assert published[0]["read_at"] is None
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)
