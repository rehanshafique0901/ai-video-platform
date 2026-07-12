"""Media bounded context (Slice α6.2).

Media assets are **generation outputs** — registered pointers to concrete
stored objects (an image/video/audio file already placed in some storage
backend). Unlike projects/scenes/prompts, a ``media_assets`` row carries its
**own** ``tenant_id`` + ``owner_user_id`` (direct ownership) and only
*optionally* links to a project/scene/prompt; it is an **owner-level artefact**,
reusable across projects, not a project child.

Like prompts (ADR-0036), media is **not** versioned editorial content: no
optimistic-concurrency token, no ``projects.version`` bump, and it is **excluded
from** version snapshots / restore / diff. Its own concurrency model is
last-writer-wins (ADR-0037, which adopts ADR-0036's model). α6.2 registers
metadata only — it makes no provider or object-storage calls.
"""
