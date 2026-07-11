"""Projects bounded-context domain package.

Houses the ``Project`` aggregate root (α5a). See
``docs/domain/PROJECT_AGGREGATE.md`` for the full aggregate model —
ownership, boundary, lifecycle, and the two distinct versioning
mechanisms (the optimistic-concurrency ``version`` counter vs the
``project_versions`` content-snapshot ledger).
"""
