"""Unit tests for the ``INotifier`` infrastructure adapters (α9.5, ADR-0051).

* :class:`LoggingNotifier` — the mock-first default always succeeds with no external I/O.
* :class:`SmtpNotifier` — its one policy job is failure classification (ADR-0051 D3): map transport
  errors onto ``NotifierDeliveryError`` with the right ``permanent`` flag. The ``aiosmtplib.send``
  call is monkeypatched (no real SMTP server / network).
"""

from __future__ import annotations

import aiosmtplib
import pytest

from app.application.interfaces.notifier import EmailMessage, NotifierDeliveryError
from app.infrastructure.notifications.logging_notifier import LoggingNotifier
from app.infrastructure.notifications.smtp_notifier import SmtpNotifier

pytestmark = pytest.mark.unit


def _message() -> EmailMessage:
    return EmailMessage(
        recipient="creator@example.com",
        subject="Your video is live",
        body_text="Congrats!",
        idempotency_key="notification-email:abc",
    )


async def test_logging_notifier_always_succeeds() -> None:
    notifier = LoggingNotifier()
    # No exception, no return value — a successful mock send.
    assert await notifier.send(_message()) is None


async def test_smtp_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_send(message: object, **kwargs: object) -> object:
        captured["message"] = message
        captured.update(kwargs)
        return None

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    notifier = SmtpNotifier(host="smtp.example.com", port=587, from_address="no-reply@example.com")

    await notifier.send(_message())

    assert captured["hostname"] == "smtp.example.com"
    assert captured["port"] == 587


@pytest.mark.parametrize(
    ("exc", "expected_permanent", "expected_code"),
    [
        (aiosmtplib.SMTPResponseException(550, "no such user"), True, "smtp_550"),
        (aiosmtplib.SMTPResponseException(451, "try again later"), False, "smtp_451"),
        (aiosmtplib.SMTPAuthenticationError(535, "bad credentials"), True, "auth_failed"),
        (aiosmtplib.SMTPServerDisconnected("connection dropped"), False, "smtp_transient"),
    ],
)
async def test_smtp_error_classification(
    monkeypatch: pytest.MonkeyPatch,
    exc: aiosmtplib.SMTPException,
    expected_permanent: bool,
    expected_code: str,
) -> None:
    async def fake_send(message: object, **kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    notifier = SmtpNotifier(host="smtp.example.com", port=587, from_address="no-reply@example.com")

    with pytest.raises(NotifierDeliveryError) as excinfo:
        await notifier.send(_message())

    assert excinfo.value.permanent is expected_permanent
    assert excinfo.value.code == expected_code


async def test_smtp_connection_error_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send(message: object, **kwargs: object) -> object:
        raise TimeoutError("timed out")

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    notifier = SmtpNotifier(host="smtp.example.com", port=587, from_address="no-reply@example.com")

    with pytest.raises(NotifierDeliveryError) as excinfo:
        await notifier.send(_message())

    assert excinfo.value.permanent is False
    assert excinfo.value.code == "connection_error"
