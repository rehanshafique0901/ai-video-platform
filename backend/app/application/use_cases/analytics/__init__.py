"""Creator Analytics Foundation (Slice α9.0).

Activates the dormant ``analytics_events`` table as a **downstream, additive outbox
consumer** (ADR-0042) that records already-completed publish/export actions, plus an
owner-scoped read model + API. Writing is **DB-enforced exactly-once** (ADR-0048): the
consumer is idempotent on ``event.id`` via ``(source_event_id, occurred_at)`` uniqueness,
with ``occurred_at = event.occurred_at`` (never ``now()``). No producer or frozen runtime
is touched.
"""
