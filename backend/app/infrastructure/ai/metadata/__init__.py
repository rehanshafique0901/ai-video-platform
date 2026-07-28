"""AI-subsystem adapters that implement Publishing-owned metadata ports (α9.1, ADR-0049).

This package holds the **only** module permitted to bridge the Publishing application plane to the
AI LLM capability. It implements ``app.application.interfaces.publish_metadata_generator`` and
depends solely on the AI capability + the neutral port DTOs — it never imports
``app.domain.publishing``, ``app.application.use_cases.publishing``, or
``app.infrastructure.publishing`` (mechanically pinned by the import-linter contract "AI plane
never imports the Publishing bounded context").
"""
