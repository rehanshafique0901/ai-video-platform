"""Print a one-line inventory of the live DB for fast sanity-checking."""

from __future__ import annotations

import _load_env
import sqlalchemy as sa

url = _load_env.load()
e = sa.create_engine(url)
with e.connect() as c:
    n_tables = c.execute(
        sa.text(
            "select count(*) from information_schema.tables "
            "where table_schema='public' and table_type='BASE TABLE'"
        )
    ).scalar()
    n_enums = c.execute(
        sa.text(
            "select count(*) from pg_type t join pg_namespace n on n.oid=t.typnamespace where t.typtype='e' and n.nspname='public'"
        )
    ).scalar()
    n_idx = c.execute(sa.text("select count(*) from pg_indexes where schemaname='public'")).scalar()
    n_trig = c.execute(
        sa.text(
            "select count(*) from pg_trigger t join pg_class c on c.oid=t.tgrelid where not t.tgisinternal and c.relnamespace=(select oid from pg_namespace where nspname='public')"
        )
    ).scalar()
    n_part_parents = c.execute(
        sa.text(
            "select count(*) from pg_partitioned_table p join pg_class c on c.oid=p.partrelid where c.relnamespace=(select oid from pg_namespace where nspname='public')"
        )
    ).scalar()
    n_part_children = c.execute(sa.text("select count(*) from pg_inherits")).scalar()
    n_funcs = c.execute(
        sa.text(
            "select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.prokind='f'"
        )
    ).scalar()
    n_fks = c.execute(
        sa.text(
            "select count(*) from information_schema.table_constraints where table_schema='public' and constraint_type='FOREIGN KEY'"
        )
    ).scalar()
    rev = c.execute(sa.text("select version_num from alembic_version")).scalar()
    seed_plans = c.execute(sa.text("select count(*) from plans")).scalar()
    seed_models = c.execute(sa.text("select count(*) from ai_models")).scalar()
    seed_flags = c.execute(sa.text("select count(*) from feature_flags")).scalar()
    seed_roles = c.execute(sa.text("select count(*) from roles")).scalar()
    seed_providers = c.execute(
        sa.text("select count(*) from provider_plugin_registrations")
    ).scalar()
    seed_settings = c.execute(sa.text("select count(*) from system_settings")).scalar()

print(f"alembic rev          : {rev}")
print(f"base tables          : {n_tables}")
print(f"enum types           : {n_enums}")
print(f"indexes              : {n_idx}")
print(f"triggers (non-int)   : {n_trig}")
print(f"partitioned parents  : {n_part_parents}")
print(f"partition children   : {n_part_children}")
print(f"functions            : {n_funcs}")
print(f"foreign keys         : {n_fks}")
print()
print("seed counts:")
print(f"  plans                          : {seed_plans}")
print(f"  ai_models                      : {seed_models}")
print(f"  feature_flags                  : {seed_flags}")
print(f"  roles                          : {seed_roles}")
print(f"  provider_plugin_registrations  : {seed_providers}")
print(f"  system_settings                : {seed_settings}")
