"""``SmtpNotifier`` — the config-gated real email transport (α9.5, ADR-0051 D4).

A config-blind, PII-minimal ``INotifier`` over ``aiosmtplib``. The composition root injects the
resolved SMTP settings (host/port/credentials/from-address/timeout) and wires this adapter **only**
when a host + from-address are present; otherwise it falls back to the ``LoggingNotifier`` (fail-soft).

Failure classification (ADR-0051 D3) is the adapter's one policy job: it maps transport errors onto
:class:`NotifierDeliveryError` with a ``permanent`` flag so the caller can choose terminal vs. bounded
retry. Permanent = invalid recipient/sender, auth/policy rejection, or a ``5xx`` server response;
transient = connect/disconnect/timeout/``4xx`` — anything worth a backed-off retry. Correctness never
depends on provider-side deduplication (ADR-0051): the ``idempotency_key`` is surfaced as a stable
``Message-ID`` for correlation only. ``asyncio.CancelledError`` is never swallowed.
"""

from __future__ import annotations

from email.message import EmailMessage as MimeMessage

import aiosmtplib
import structlog

from app.application.interfaces.notifier import (
    EmailMessage,
    INotifier,
    NotifierDeliveryError,
)

_LOGGER = structlog.get_logger(__name__)


class SmtpNotifier(INotifier):
    """Send email over SMTP via ``aiosmtplib`` (config-gated; wired only when SMTP is configured)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        from_address: str,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 30.0,
        use_tls: bool = False,
        start_tls: bool | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._from_address = from_address
        self._username = username
        self._password = password
        self._timeout_seconds = timeout_seconds
        # Implicit TLS (SMTPS, usually port 465) vs. opportunistic STARTTLS (usually 587). When
        # ``start_tls`` is left unset we default to STARTTLS unless implicit TLS was requested.
        self._use_tls = use_tls
        self._start_tls = start_tls if start_tls is not None else (not use_tls)

    async def send(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"] = self._from_address
        mime["To"] = message.recipient
        mime["Subject"] = message.subject
        # Correlation-first (ADR-0051): a stable, notification-derived Message-ID. Some providers may
        # additionally use it for dedup, but the platform never relies on that (Appendix A).
        mime["Message-ID"] = f"<{message.idempotency_key}@{self._message_id_domain()}>"
        mime.set_content(message.body_text)

        try:
            await aiosmtplib.send(
                mime,
                hostname=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                timeout=self._timeout_seconds,
                use_tls=self._use_tls,
                start_tls=self._start_tls,
            )
        except aiosmtplib.SMTPException as exc:
            permanent, code = _classify(exc)
            _LOGGER.warning(
                "notification.email.smtp_error",
                transport="smtp",
                recipient=_mask(message.recipient),
                idempotency_key=message.idempotency_key,
                code=code,
                permanent=permanent,
            )
            raise NotifierDeliveryError(str(exc), permanent=permanent, code=code) from exc
        except (TimeoutError, OSError) as exc:
            # Connection-level failures are transient — worth a backed-off retry (ADR-0051 D3).
            _LOGGER.warning(
                "notification.email.smtp_error",
                transport="smtp",
                recipient=_mask(message.recipient),
                idempotency_key=message.idempotency_key,
                code="connection_error",
                permanent=False,
            )
            raise NotifierDeliveryError(str(exc), permanent=False, code="connection_error") from exc

    def _message_id_domain(self) -> str:
        _, _, domain = self._from_address.partition("@")
        return domain or "localhost"


def _classify(exc: aiosmtplib.SMTPException) -> tuple[bool, str]:
    """Map an ``aiosmtplib`` error onto ``(permanent, code)`` per ADR-0051 D3."""
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused | aiosmtplib.SMTPSenderRefused):
        return True, "address_refused"
    if isinstance(exc, aiosmtplib.SMTPAuthenticationError):
        return True, "auth_failed"
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        code = getattr(exc, "code", 0) or 0
        if code >= 500:
            return True, f"smtp_{code}"
        return False, f"smtp_{code}"
    # Connect/disconnect/timeout/helo/not-supported → transient.
    return False, "smtp_transient"


def _mask(email: str) -> str:
    """Mask an address for logs (ADR-0051 D4): ``a***@example.com`` — never the full local part."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = local[0] if local else ""
    return f"{head}***@{domain}"
