"""Port: outbound notifier (email) — owned by the application layer (α9.5, ADR-0051).

This is the application-owned seam behind which an infrastructure adapter sends one already-rendered
message to an external channel (email in v1). Per ADR-0051 (D4/D5):

* The port + its DTO are **neutral** — they carry only a ready-to-send message and reference no
  infrastructure, no provider type, and no ``notifications`` persistence. The dependency direction is
  strictly one-way (the application owns the abstraction; an infrastructure adapter supplies the
  implementation).
* The adapter is a **config-blind, PII-minimal leaf** (mirrors ADR-0047 credential-blindness): the
  application resolves the recipient (owner-scoped) and hands the adapter a ready :class:`EmailMessage`;
  the adapter resolves nothing, stores nothing, and MUST NOT log the full recipient address or body.
* Correctness never depends on provider-side deduplication (ADR-0051 Appendix A is non-normative):
  ``idempotency_key`` is a **correlation/reconciliation** identifier the adapter may additionally use
  as a provider dedup key **only where a provider is independently verified to honour it**.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """A neutral, ready-to-send email. The adapter resolves nothing (ADR-0051 D4).

    ``recipient`` is the resolved ``User.email`` — the only PII that crosses the boundary, together
    with ``subject`` / ``body_text``. ``idempotency_key`` is a deterministic, notification-derived
    identifier (stable across retries) minted by the caller; it is correlation-first (ADR-0051) —
    an adapter may surface it as a stable ``Message-ID`` / custom header, and may use it as a native
    dedup key only for a provider verified to honour one.
    """

    recipient: str
    subject: str
    body_text: str
    idempotency_key: str


class NotifierDeliveryError(RuntimeError):
    """Raised by an adapter when a send fails.

    ``permanent`` classifies the failure so the caller can choose retry vs. terminal (ADR-0051 D3):
    a permanent failure (invalid address / hard bounce / auth-or-policy rejection) is terminal and
    must not be retried; a transient failure (network / timeout / rate-limit / transient server
    error) is eligible for a bounded, backed-off retry. ``code`` is a coarse, non-PII reason label
    safe to log.
    """

    def __init__(self, message: str, *, permanent: bool, code: str) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.code = code


class INotifier(ABC):
    @abstractmethod
    async def send(self, message: EmailMessage) -> None:
        """Send one email, or raise :class:`NotifierDeliveryError`.

        Implementations MUST be **cancellation-safe** — if the awaiting task is cancelled, the
        underlying transport call is cancelled cleanly and ``asyncio.CancelledError`` propagates
        (never swallowed, no leaked background task). Implementations MUST NOT log the full recipient
        address, subject, or body (ADR-0051 D4); a **masked** recipient + the ``idempotency_key`` +
        a coarse status are the only permitted delivery telemetry.
        """
        ...


__all__ = ["EmailMessage", "INotifier", "NotifierDeliveryError"]
