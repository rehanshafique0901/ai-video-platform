"""Infrastructure adapters implementing the application-owned ``INotifier`` port (α9.5, ADR-0051).

These are the only modules permitted to talk to an outbound email transport. They are **config-blind,
PII-minimal leaves** (ADR-0051 D4, mirroring ADR-0047 credential-blindness): the application resolves
the recipient and hands each adapter a ready :class:`~app.application.interfaces.notifier.EmailMessage`;
the adapter resolves nothing, persists nothing, and never logs the full recipient / subject / body.

* :class:`~app.infrastructure.notifications.logging_notifier.LoggingNotifier` — the mock-first default
  (dev + CI): emits a masked delivery log and always succeeds. Deterministic, no external I/O.
* :class:`~app.infrastructure.notifications.smtp_notifier.SmtpNotifier` — the config-gated real
  transport (``aiosmtplib``). Only wired when SMTP settings are present; otherwise the composition root
  falls back to :class:`LoggingNotifier` (fail-soft — email delivery is best-effort, never a boot gate).
"""
