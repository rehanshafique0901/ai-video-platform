"""``LoggingNotifier`` — the mock-first, always-succeeds ``INotifier`` (α9.5, ADR-0051 D5).

The default notifier for dev + CI (and the fail-soft fallback when no SMTP transport is configured).
It performs no external I/O: it emits a single **masked** delivery log (never the full address, subject,
or body — ADR-0051 D4) and returns success. This keeps the whole email-delivery pipeline deterministic
under the CI gate — the worker/lease/send-then-stamp/read-model-sanitisation logic is exercised end to
end without a live SMTP server or any network flakiness.
"""

from __future__ import annotations

import structlog

from app.application.interfaces.notifier import EmailMessage, INotifier

_LOGGER = structlog.get_logger(__name__)


class LoggingNotifier(INotifier):
    """An ``INotifier`` that records a masked send and always succeeds (no external I/O)."""

    async def send(self, message: EmailMessage) -> None:
        _LOGGER.info(
            "notification.email.sent",
            transport="logging",
            recipient=_mask(message.recipient),
            idempotency_key=message.idempotency_key,
        )


def _mask(email: str) -> str:
    """Mask an address for logs (ADR-0051 D4): ``a***@example.com`` — never the full local part."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = local[0] if local else ""
    return f"{head}***@{domain}"
