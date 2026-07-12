"""Prompt use cases (Slice α6.1) — create / list / get / update / delete.

Every prompt use case first runs the **project ownership gate** (α6.1 D2,
mirroring α5c D6): ``uow.projects.get_owned(project_id, tenant_id,
owner_user_id)`` → 404 if the caller does not own a live project with that id.
Only then does it touch the prompt surface, which is itself project-scoped —
the two-level visibility gate that keeps a caller from reading or mutating
prompts under another owner's project by guessing ids (anti-enumeration).

Per ADR-0036 (Q1 = Option A), prompts are **generation inputs**, not versioned
editorial content: no use case here bumps ``projects.version`` and none carries
an optimistic-concurrency fence. A prompt edit is last-writer-wins.
"""
