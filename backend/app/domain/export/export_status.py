"""``ExportStatus`` — the export-job lifecycle enum (Slice α8.5a).

Framework-free mirror of the physical ``export_status`` ENUM (``schema.md`` §18 /
``enums.py``). Mirrors :class:`app.domain.render.render_status.RenderStatus` exactly —
the export worker drives the same lifecycle as the render worker:

    queued ─▶ running ─▶ succeeded
       │        ├─▶ failed
       └────────┴─▶ canceled

``running`` / ``succeeded`` / ``failed`` are producible only by the background export
worker; ``CreateExportJob`` creates jobs in ``queued``.
"""

from __future__ import annotations

from enum import StrEnum


class ExportStatus(StrEnum):
    """The five ``export_status`` values."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        """True for states that no longer transition (``succeeded``/``failed``/``canceled``)."""
        return self in _TERMINAL


_TERMINAL: frozenset[ExportStatus] = frozenset(
    {ExportStatus.SUCCEEDED, ExportStatus.FAILED, ExportStatus.CANCELED}
)
