"""Project Version use cases (Slice α5d.1) — create / list / get.

Every version use case first runs the **project ownership gate** (mirrors
α5c): ``uow.projects.get_owned(project_id, tenant_id, owner_user_id)`` → 404
if the caller does not own a live project with that id. Only then does it
touch the append-only ``project_versions`` ledger, which is itself
project-scoped. This keeps a caller from capturing, listing, or reading
snapshots under another owner's project by guessing ids (anti-enumeration,
inherited from α3/α5a).

α5d.1 ships the read side of the version lifecycle (capture + browse); the
mutating operations that consume a snapshot — ``restore`` and ``branch`` —
are deferred to α5d.2 (pre-flight Q1).
"""
