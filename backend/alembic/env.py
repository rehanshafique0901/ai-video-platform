"""Alembic environment for the AI Video Platform.

Phase 2 Step B foundation. Configured for offline + online runs against
PostgreSQL 15+ with required extensions (pgcrypto, citext, pg_trgm, vector,
btree_gin). The metadata is taken from `app.infrastructure.db.base.metadata`
which collects every ORM model declared in `app/infrastructure/db/models/`.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.infrastructure.db import models  # noqa: F401  - registers every model
from app.infrastructure.db.base import metadata as target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DATABASE_URL = os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url"))
if DATABASE_URL:
    # ConfigParser interprets `%` as an interpolation token; double it so URL-
    # encoded password characters (e.g. `%40` for `@`) survive round-tripping.
    config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))


def include_object(obj, name, type_, reflected, compare_to):
    """Skip Postgres partition children from autogenerate (handled manually)."""
    return not (
        type_ == "table"
        and name.startswith(("usage_records_y", "analytics_events_y", "event_log_y", "audit_log_y"))
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
