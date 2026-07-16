"""``RenderStatus`` — the render-job lifecycle enum (Slice α7.1).

Framework-free mirror of the physical ``render_status`` ENUM (``schema.md`` §17 /
``enums.py``). Kept as a domain type (not bare string literals) so the use cases
guard transitions in the domain layer. The α7.1 legal transitions are:

    queued ─▶ running ─▶ succeeded
       │        ├─▶ failed
       └────────┴─▶ canceled

``running`` / ``succeeded`` / ``failed`` are producible only by the background
render worker (α8.x); α7.1 creates jobs in ``queued`` and moves them to
``canceled`` (from ``queued`` or ``running``). The blueprint state machine is
§4.3 of ``docs/architecture/CONTENT_GENERATION_PIPELINE.md``.
"""

from __future__ import annotations

from enum import StrEnum


class RenderStatus(StrEnum):
    """The five ``render_status`` values."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        """True for states that no longer transition (``succeeded``/``failed``/``canceled``)."""
        return self in _TERMINAL

    @property
    def is_cancelable(self) -> bool:
        """True iff a cancel request may move this state to ``canceled``.

        ``queued`` and ``running`` are cancelable; ``canceled`` is idempotently
        cancelable (a no-op re-cancel, handled by the use case); ``succeeded`` /
        ``failed`` are **not** (completed work → ``409``, α7.1 Q3/D3.6).
        """
        return self in _CANCELABLE


_TERMINAL: frozenset[RenderStatus] = frozenset(
    {RenderStatus.SUCCEEDED, RenderStatus.FAILED, RenderStatus.CANCELED}
)
_CANCELABLE: frozenset[RenderStatus] = frozenset({RenderStatus.QUEUED, RenderStatus.RUNNING})
