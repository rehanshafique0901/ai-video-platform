"""Execution Runtime lifecycle event types (α8.6 Increment 4).

Internal events emitted through the transactional outbox as the generation
lifecycle advances (EXECUTION_RUNTIME_CONTRACT.md §6). They have no external
consumers yet; they exist so UI / telemetry / analytics / notifications can
subscribe later without touching execution code.
"""

from __future__ import annotations

AGGREGATE_TYPE = "generation"

EVENT_GENERATION_STARTED = "generation.started"
EVENT_SHOT_GENERATED = "generation.shot_generated"
EVENT_VERIFICATION_FAILED = "generation.verification_failed"
EVENT_REPAIR_SUCCEEDED = "generation.repair_succeeded"
EVENT_VIDEO_RENDERED = "generation.video_rendered"
EVENT_EXPORT_COMPLETED = "generation.export_completed"
