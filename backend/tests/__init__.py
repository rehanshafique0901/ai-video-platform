"""Backend test suite.

Two kinds of tests live here, separated by pytest marker:

* ``unit``        — pure-Python, no DB. Run in stage 4 of the CI gate.
* ``integration`` — require a live PostgreSQL with pgvector. Run in
                    stages 5–9 (alembic up/down/up + schema validator +
                    ERD diff) via the harness in ``backend/scripts/``.

Stage 4 is intentionally fast (< 2 s) so a developer can iterate locally
without spinning up Postgres. Integration coverage is owned by the live
validator and proven on Supabase / pgvector in `SCHEMA_VALIDATION.md`.
"""
