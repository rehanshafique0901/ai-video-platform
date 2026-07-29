"""``NotificationEmailWorker`` — the dedicated poll ingress for email delivery (α9.5, ADR-0051 D2-C).

Email delivery is an out-of-band external effect, so — like transcode/export/publish — it runs behind
a **dedicated poller**, never the relay fan-out (which owns only the in-app projection write). One
``run_once()`` scans the oldest undelivered, non-terminal, backoff-eligible notifications (FIFO) and
hands each to :class:`ProcessNotificationEmail`, which delivers it independently under its own
``notification_email:<id>`` lease — so one slow/failing recipient never blocks the others.

The worker reads only ``notifications`` (the deliverable scan) and delegates; it performs no
orchestration and mutates no publish/export/render state. A single misbehaving row is isolated: its
exception is logged and the scan continues (at-least-once — the row is retried next poll).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.notifications.process_notification_email import (
    ProcessNotificationEmail,
    ProcessNotificationEmailResult,
)

_LOGGER = structlog.get_logger(__name__)

_DEFAULT_BATCH = 20


@dataclass(frozen=True, slots=True)
class NotificationEmailPollResult:
    """Aggregate outcome of one ``run_once`` scan."""

    scanned: int
    outcomes: list[ProcessNotificationEmailResult] = field(default_factory=list)


class NotificationEmailWorker:
    """Drain deliverable notification emails once per invocation (the α9.5 email ingress)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        process: ProcessNotificationEmail,
        *,
        batch_size: int = _DEFAULT_BATCH,
    ) -> None:
        self._uow = uow
        self._process = process
        self._batch_size = batch_size

    async def run_once(self) -> NotificationEmailPollResult:
        """Send every currently-deliverable notification email in the batch, oldest first."""
        now = datetime.now(UTC)
        async with self._uow:
            claims = await self._uow.notifications.list_email_deliverable(
                now=now, limit=self._batch_size
            )

        outcomes: list[ProcessNotificationEmailResult] = []
        for claim in claims:
            try:
                outcomes.append(await self._process.process(claim))
            except Exception:
                # At-least-once: an unexpected error leaves the row unstamped, so it is retried
                # next poll. Never let one row abort the batch.
                _LOGGER.warning(
                    "notification.email.process_error",
                    notification_id=str(claim.id),
                    exc_info=True,
                )
        return NotificationEmailPollResult(scanned=len(claims), outcomes=outcomes)
