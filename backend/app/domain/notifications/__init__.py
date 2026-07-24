"""Notifications domain — the in-app notification projection (Slice α8.5b.3).

A **notification** is product state projected from an immutable, already-committed
export terminal event (``ExportJobSucceeded`` / ``ExportJobFailed``). Creation is a
pure projection (W8.5b.6) and is exactly-once per recipient per source event, enforced
by the persistence layer (W8.5b.7).
"""

from __future__ import annotations

from app.domain.notifications.notification import Notification

__all__ = ["Notification"]
