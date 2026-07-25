"""Execution Runtime lifecycle + artefact vocabulary (α8.6 Increment 4).

These enums mirror the Postgres enums created by migration ``0012`` one-for-one
(``generation_status``, ``generation_asset_kind``) and are the single source of
truth the Execution Runtime uses when persisting state. See
``docs/engineering/EXECUTION_RUNTIME_CONTRACT.md`` §4 (state machine) and §3.
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionStatus(StrEnum):
    """Persisted generation lifecycle (``generations.status``).

    The only backward edge is ``VERIFYING`` <-> ``REPAIRING`` (per shot);
    ``COMPLETED`` / ``FAILED`` / ``CANCELLED`` are terminal.
    """

    QUEUED = "queued"
    PLANNING = "planning"
    RESOLVING = "resolving"
    GENERATING = "generating"
    VERIFYING = "verifying"
    REPAIRING = "repairing"
    RENDERING = "rendering"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GenerationAssetKind(StrEnum):
    """Kind of execution artefact (``generation_assets.asset_kind``)."""

    FRAME = "frame"
    REFERENCE = "reference"
    MASK = "mask"
    AUDIO = "audio"
    VIDEO = "video"
    THUMBNAIL = "thumbnail"
    METADATA = "metadata"
