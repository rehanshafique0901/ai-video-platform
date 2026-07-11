"""Scene use cases (Slice α5c) — create / list / get / update / move / delete.

Every scene use case first runs the **project ownership gate** (α5c D6):
``uow.projects.get_owned(project_id, tenant_id, owner_user_id)`` → 404 if
the caller does not own a live project with that id. Only then does it
touch the scene surface, which is itself project-scoped. This two-level
visibility gate keeps a caller from reading or mutating scenes under another
owner's project by guessing ids (anti-enumeration, inherited from α3/α5a).
"""
