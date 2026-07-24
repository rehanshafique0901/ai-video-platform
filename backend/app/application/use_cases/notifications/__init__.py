"""Notification use cases (Slice α8.5b.3 write + α8.5b.3r read).

α8.5b.3 shipped the **write half**: project export terminal events (``ExportJobSucceeded``
/ ``ExportJobFailed``) into exactly-once in-app notification rows. α8.5b.3r adds the
**read/query half**: list (keyset-paginated), unread count, mark-read (single), and
mark-all-read — owner-scoped, metadata-only, order-stable (W8.5b.8/9/10).
"""

from __future__ import annotations

from app.application.use_cases.notifications.count_unread_notifications import (
    CountUnreadNotifications,
)
from app.application.use_cases.notifications.create_notification import (
    CreateNotification,
    CreateNotificationResult,
)
from app.application.use_cases.notifications.list_notifications import ListNotifications
from app.application.use_cases.notifications.mark_all_notifications_read import (
    MarkAllNotificationsRead,
)
from app.application.use_cases.notifications.mark_notification_read import (
    MarkNotificationRead,
)
from app.application.use_cases.notifications.notification_projection import (
    NotificationProjection,
)

__all__ = [
    "CountUnreadNotifications",
    "CreateNotification",
    "CreateNotificationResult",
    "ListNotifications",
    "MarkAllNotificationsRead",
    "MarkNotificationRead",
    "NotificationProjection",
]
