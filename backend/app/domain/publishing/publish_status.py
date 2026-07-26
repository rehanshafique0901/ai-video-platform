"""``PublishStatus`` — the publish-runtime job lifecycle (α8.6b).

A faithful adaptation of :class:`app.domain.export.export_status.ExportStatus` (DQ8): the
same five values and terminal set. Scheduling (``scheduled_at``) and bounded retries
(``attempt`` / ``max_attempts``) are **columns**, not states — a job awaiting its schedule
or its next retry is simply ``QUEUED`` (mirrors the export/worker poll pattern).
"""

from __future__ import annotations

from enum import StrEnum


class PublishStatus(StrEnum):
    """The five ``publish_status`` values (mirrors ``export_status``)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        """True for states that no longer transition (``succeeded``/``failed``/``canceled``)."""
        return self in _TERMINAL


_TERMINAL: frozenset[PublishStatus] = frozenset(
    {PublishStatus.SUCCEEDED, PublishStatus.FAILED, PublishStatus.CANCELED}
)


__all__ = ["PublishStatus"]
