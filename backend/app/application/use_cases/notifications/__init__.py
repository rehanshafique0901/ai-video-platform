"""Notification projection use cases (Slice α8.5b.3).

The write half of the distribution context's notification capability: project export
terminal events (``ExportJobSucceeded`` / ``ExportJobFailed``) into exactly-once in-app
notification rows. The read/query API is deferred to α8.5b.3r.
"""

from __future__ import annotations

from app.application.use_cases.notifications.create_notification import (
    CreateNotification,
    CreateNotificationResult,
)
from app.application.use_cases.notifications.notification_projection import (
    NotificationProjection,
)

__all__ = [
    "CreateNotification",
    "CreateNotificationResult",
    "NotificationProjection",
]
