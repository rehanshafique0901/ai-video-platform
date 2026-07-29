"""``ProcessNotificationEmail`` — deliver one notification's email under a lease (α9.5, ADR-0051).

The per-row body behind :class:`NotificationEmailWorker`. For one claimed, undelivered notification it:

1. acquires the ``notification_email:<id>`` lease (D2-C serialisation — one sender per row at a time);
   if unavailable it skips cleanly (another worker/pass owns it; no attempt burned);
2. resolves the recipient **owner-scoped** (``users.get_by_id`` → ``User.email``); a vanished user is
   a permanent, terminal failure (the notification can never be emailed);
3. builds a neutral :class:`EmailMessage` (the only PII crossing the boundary) with a deterministic,
   notification-derived ``idempotency_key`` (stable across retries — correlation-first, ADR-0051);
4. **sends outside any DB transaction**, then stamps (send-then-stamp, D1-C): success →
   ``mark_email_delivered``; a transient failure with attempts remaining → a backed-off retry (D3);
   a permanent failure, or the attempt ceiling, → a terminal failure recorded in the reserved
   ``payload["_email"]`` namespace.

Invariants: the send happens with **no lock held across the network call**; correctness never relies
on provider-side deduplication (ADR-0051) — the lease + send-then-stamp give at-least-once delivery
with a bounded, explicitly-accepted rare-duplicate window. The reserved ``_email`` bookkeeping is an
implementation detail (never the public contract); it is stripped from every read-model row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog

from app.application.interfaces.notifier import (
    EmailMessage,
    INotifier,
    NotifierDeliveryError,
)
from app.application.interfaces.repositories import NotificationEmailDelivery
from app.application.interfaces.unit_of_work import IUnitOfWork

_LOGGER = structlog.get_logger(__name__)

_DEFAULT_LEASE = timedelta(seconds=120)
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BACKOFF_BASE_SECONDS = 60
_DEFAULT_BACKOFF_CAP_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ProcessNotificationEmailResult:
    """Outcome of one email-delivery pass over a single notification."""

    notification_id: UUID
    status: str  # "delivered" | "retry" | "failed" | "skipped"
    reason: str | None = None


class ProcessNotificationEmail:
    """Deliver one notification's email under a per-notification lease (send-then-stamp)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        notifier: INotifier,
        *,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: int = _DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: int = _DEFAULT_BACKOFF_CAP_SECONDS,
        lease: timedelta = _DEFAULT_LEASE,
        owner: str | None = None,
    ) -> None:
        self._uow = uow
        self._notifier = notifier
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_cap_seconds = backoff_cap_seconds
        self._lease = lease
        self._owner = owner or f"notification-email-worker:{uuid4()}"

    async def process(self, claim: NotificationEmailDelivery) -> ProcessNotificationEmailResult:
        # Lease — one sender per notification at a time (D2-C). If held, skip cleanly (no burn).
        async with self._uow:
            lease = await self._uow.locks.acquire(
                key=f"notification_email:{claim.id}", owner=self._owner, lease=self._lease
            )
            await self._uow.commit()
        if lease is None:
            _LOGGER.info("notification.email.locked", notification_id=str(claim.id))
            return ProcessNotificationEmailResult(
                notification_id=claim.id, status="skipped", reason="locked"
            )

        try:
            return await self._deliver(claim)
        finally:
            async with self._uow:
                await self._uow.locks.release(lease)
                await self._uow.commit()

    async def _deliver(self, claim: NotificationEmailDelivery) -> ProcessNotificationEmailResult:
        # Resolve the recipient owner-scoped. A vanished user is permanent (never emailable).
        async with self._uow:
            user = await self._uow.users.get_by_id(claim.user_id)
        recipient = getattr(user, "email", None)
        if user is None or not recipient:
            return await self._record_failure(claim, permanent=True, code="recipient_unresolved")

        message = EmailMessage(
            recipient=recipient,
            subject=claim.title,
            body_text=claim.body or "",
            # Deterministic + stable across retries (correlation-first, ADR-0051 D1-C).
            idempotency_key=f"notification-email:{claim.id}",
        )

        # Send OUTSIDE any transaction (no lock held across the network call).
        try:
            await self._notifier.send(message)
        except NotifierDeliveryError as exc:
            return await self._record_failure(claim, permanent=exc.permanent, code=exc.code)

        # Send-then-stamp: only a confirmed send stamps ``delivered_email_at`` (D1-C).
        async with self._uow:
            await self._uow.notifications.mark_email_delivered(notification_id=claim.id)
            await self._uow.commit()
        _LOGGER.info(
            "notification.email.delivered",
            notification_id=str(claim.id),
            recipient=_mask(recipient),
        )
        return ProcessNotificationEmailResult(notification_id=claim.id, status="delivered")

    async def _record_failure(
        self, claim: NotificationEmailDelivery, *, permanent: bool, code: str
    ) -> ProcessNotificationEmailResult:
        attempts = claim.attempts + 1
        terminal = permanent or attempts >= self._max_attempts
        next_attempt_at = None if terminal else datetime.now(UTC) + self._backoff(attempts)
        async with self._uow:
            await self._uow.notifications.record_email_delivery_failure(
                notification_id=claim.id,
                terminal=terminal,
                code=code,
                attempts=attempts,
                next_attempt_at=next_attempt_at,
            )
            await self._uow.commit()
        if terminal:
            _LOGGER.warning(
                "notification.email.failed",
                notification_id=str(claim.id),
                attempts=attempts,
                code=code,
                permanent=permanent,
            )
            return ProcessNotificationEmailResult(
                notification_id=claim.id, status="failed", reason=code
            )
        _LOGGER.info(
            "notification.email.retry_scheduled",
            notification_id=str(claim.id),
            attempts=attempts,
            max_attempts=self._max_attempts,
            code=code,
            next_attempt_at=next_attempt_at.isoformat() if next_attempt_at else None,
        )
        return ProcessNotificationEmailResult(notification_id=claim.id, status="retry", reason=code)

    def _backoff(self, attempts: int) -> timedelta:
        seconds = min(
            self._backoff_cap_seconds,
            self._backoff_base_seconds * (2 ** max(0, attempts - 1)),
        )
        return timedelta(seconds=seconds)


def _mask(email: str) -> str:
    """Mask an address for logs (ADR-0051 D4): ``a***@example.com`` — never the full local part."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = local[0] if local else ""
    return f"{head}***@{domain}"
