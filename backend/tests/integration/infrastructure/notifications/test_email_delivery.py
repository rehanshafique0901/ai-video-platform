"""α9.5 — Notification Delivery (Email) end-to-end against live PostgreSQL (ADR-0051).

Proves the additive, out-of-band email pipeline works across the real transaction model:

* the ``NotificationEmailWorker`` (wired via the container with the mock ``LoggingNotifier``) drains an
  undelivered notification, and under a per-notification lease sends + stamps ``delivered_email_at``
  (send-then-stamp) — a delivered row is then never re-scanned;
* a **transient** send failure records backed-off retry bookkeeping in the reserved
  ``payload["_email"]`` namespace (``delivered_email_at`` untouched), and a **permanent** failure /
  the **attempt ceiling** marks the row terminally failed (never re-scanned);
* crucially, the reserved ``_email`` bookkeeping is **stripped from the read model / public API**
  (centralised at the repository row→entity boundary) even though it is present in the stored row.

Like the projection slice, ``CreateNotification`` / the worker commit their own UoWs, so these tests
seed committed rows and clean them up on teardown (deleting the user cascades notifications). Each
test uses a unique user, so the global (non-owner-scoped) delivery scan never crosses tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.notifier import (
    EmailMessage,
    INotifier,
    NotifierDeliveryError,
)
from app.application.interfaces.repositories import NotificationEmailDelivery
from app.application.use_cases.notifications.create_notification import CreateNotification
from app.application.use_cases.notifications.notification_email_worker import (
    NotificationEmailWorker,
)
from app.application.use_cases.notifications.process_notification_email import (
    ProcessNotificationEmail,
)
from app.core import container
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.notifications.logging_notifier import LoggingNotifier
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Notifiers                                                                    #
# --------------------------------------------------------------------------- #
class _FailingNotifier(INotifier):
    def __init__(self, *, permanent: bool, code: str = "boom") -> None:
        self._permanent = permanent
        self._code = code

    async def send(self, message: EmailMessage) -> None:
        raise NotifierDeliveryError("send failed", permanent=self._permanent, code=self._code)


# --------------------------------------------------------------------------- #
# Seeding / cleanup / query helpers (committed; the worker commits its own UoW)#
# --------------------------------------------------------------------------- #
async def _seed_user(session_factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    tenant_id = uuid4()
    user_id = uuid4()
    async with session_factory() as s:
        await s.execute(insert(Tenant).values(id=tenant_id, name="EM", slug=f"em-{tenant_id}"))
        await s.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"em-{user_id}@example.com",
                display_name="Email Owner",
            )
        )
        await s.commit()
    return tenant_id, user_id


async def _cleanup_user(
    session_factory: async_sessionmaker[AsyncSession], *, tenant_id: UUID, user_id: UUID
) -> None:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = CAST(:u AS uuid)"), {"u": str(user_id)})
        await s.execute(
            text("DELETE FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": str(tenant_id)}
        )
        await s.commit()


async def _create_notification(
    session_factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> UUID:
    """Commit one in-app notification (delivered_email_at NULL) via the reused writer."""
    cn = CreateNotification(SqlAlchemyUnitOfWork(session_factory))
    result = await cn.execute(
        user_id=user_id,
        kind="publish.succeeded",
        title="Your video is live",
        body="Your YouTube upload succeeded.",
        payload={"platform": "youtube"},
        source_event_id=uuid4(),
    )
    assert result.notification_id is not None
    return result.notification_id


async def _row(session_factory: async_sessionmaker[AsyncSession], notification_id: UUID) -> dict:
    async with session_factory() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT delivered_email_at, payload FROM notifications "
                        "WHERE id = CAST(:i AS uuid)"
                    ),
                    {"i": str(notification_id)},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _deliverable_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> set[UUID]:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        claims = await uow.notifications.list_email_deliverable(now=datetime.now(UTC), limit=1000)
    return {c.id for c in claims}


def _process_with(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: INotifier,
    *,
    max_attempts: int = 5,
) -> ProcessNotificationEmail:
    return ProcessNotificationEmail(
        uow=SqlAlchemyUnitOfWork(session_factory),
        notifier=notifier,
        max_attempts=max_attempts,
        backoff_base_seconds=60,
        backoff_cap_seconds=3600,
        lease=timedelta(seconds=120),
    )


def _claim(notification_id: UUID, user_id: UUID) -> NotificationEmailDelivery:
    return NotificationEmailDelivery(
        id=notification_id,
        user_id=user_id,
        title="Your video is live",
        body="Your YouTube upload succeeded.",
        attempts=0,
    )


# --------------------------------------------------------------------------- #
# Delivery (send-then-stamp) via the container-wired worker (LoggingNotifier)  #
# --------------------------------------------------------------------------- #
async def test_worker_delivers_and_stamps_then_never_rescans(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_user(session_factory)
    try:
        notification_id = await _create_notification(session_factory, user_id)
        # Before delivery the row is deliverable.
        assert notification_id in await _deliverable_ids(session_factory)

        # The worker wired exactly as the composition root does under the gate (LoggingNotifier).
        worker = NotificationEmailWorker(
            uow=SqlAlchemyUnitOfWork(session_factory),
            process=_process_with(session_factory, LoggingNotifier()),
            batch_size=20,
        )
        result = await worker.run_once()

        delivered = [o for o in result.outcomes if o.notification_id == notification_id]
        assert delivered and delivered[0].status == "delivered"

        row = await _row(session_factory, notification_id)
        assert row["delivered_email_at"] is not None  # send-then-stamp
        assert "_email" not in row["payload"]  # success writes no bookkeeping

        # A delivered row is never re-scanned.
        assert notification_id not in await _deliverable_ids(session_factory)
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)


# --------------------------------------------------------------------------- #
# Transient failure → backed-off retry bookkeeping in the reserved namespace   #
# --------------------------------------------------------------------------- #
async def test_transient_failure_records_retry_in_reserved_namespace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_user(session_factory)
    try:
        notification_id = await _create_notification(session_factory, user_id)
        process = _process_with(session_factory, _FailingNotifier(permanent=False, code="smtp_451"))

        result = await process.process(_claim(notification_id, user_id))
        assert result.status == "retry"

        row = await _row(session_factory, notification_id)
        # Not delivered; bookkeeping recorded in the reserved namespace.
        assert row["delivered_email_at"] is None
        email = row["payload"]["_email"]
        assert email["state"] == "pending"
        assert email["attempts"] == 1
        assert email["last_error"] == "smtp_451"
        assert email["next_attempt_at"] is not None

        # The backoff gate is in the future, so the row is NOT immediately re-scanned.
        assert notification_id not in await _deliverable_ids(session_factory)
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)


# --------------------------------------------------------------------------- #
# Permanent failure + attempt ceiling → terminal, never re-scanned            #
# --------------------------------------------------------------------------- #
async def test_permanent_failure_is_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_user(session_factory)
    try:
        notification_id = await _create_notification(session_factory, user_id)
        process = _process_with(
            session_factory, _FailingNotifier(permanent=True, code="address_refused")
        )

        result = await process.process(_claim(notification_id, user_id))
        assert result.status == "failed"

        row = await _row(session_factory, notification_id)
        assert row["delivered_email_at"] is None
        assert row["payload"]["_email"]["state"] == "failed"
        # Terminal rows are excluded from the deliverable scan.
        assert notification_id not in await _deliverable_ids(session_factory)
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)


async def test_attempt_ceiling_makes_transient_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_user(session_factory)
    try:
        notification_id = await _create_notification(session_factory, user_id)
        # max_attempts=1 → the first transient failure already reaches the ceiling → terminal.
        process = _process_with(session_factory, _FailingNotifier(permanent=False), max_attempts=1)

        result = await process.process(_claim(notification_id, user_id))
        assert result.status == "failed"

        row = await _row(session_factory, notification_id)
        assert row["payload"]["_email"]["state"] == "failed"
        assert notification_id not in await _deliverable_ids(session_factory)
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)


# --------------------------------------------------------------------------- #
# The reserved _email bookkeeping is NEVER exposed via the read API           #
# --------------------------------------------------------------------------- #
async def test_reserved_email_namespace_stripped_from_read_api(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reg = container.get_register_user_use_case()
    result = await reg.execute(
        email=f"em-api-{uuid4()}@example.com",
        password="correct horse battery staple",
        name="Email API",
    )
    user_id = result.user.id
    tenant_id = result.tenant.id
    auth = {"Authorization": f"Bearer {result.tokens.access_token}"}
    try:
        notification_id = await _create_notification(session_factory, user_id)
        # Force a transient failure so the reserved ``_email`` namespace IS written to the row.
        process = _process_with(session_factory, _FailingNotifier(permanent=False, code="smtp_451"))
        await process.process(_claim(notification_id, user_id))

        # It is present in the stored row ...
        row = await _row(session_factory, notification_id)
        assert "_email" in row["payload"]

        # ... but the read API's public payload never exposes it.
        resp = await client.get("/api/v1/notifications", headers=auth)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        mine = [n for n in items if n["id"] == str(notification_id)]
        assert len(mine) == 1
        payload = mine[0]["payload"]
        assert "_email" not in payload
        assert payload["platform"] == "youtube"  # public keys preserved unchanged
    finally:
        await _cleanup_user(session_factory, tenant_id=tenant_id, user_id=user_id)
