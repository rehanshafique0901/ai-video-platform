"""Identity bounded context — domain entities.

Framework-free, dependency-free. These are frozen dataclasses; they own
no persistence and no HTTP concerns. Schema mirror: ``docs/database/
schema.md`` §1–§5. ORM equivalents live under
``app/infrastructure/db/models/identity.py`` and are intentionally
distinct from these domain entities per the layered architecture
(see ARCHITECTURE.md §3 and CONTRIBUTING.md §3).

Slice α2a introduces ``User``, ``Tenant``, ``Session``; later slices
extend the package with ``OAuthIdentity`` (α5) and ``Role`` if / when
role behaviour outgrows the current lookup-table pattern.
"""
