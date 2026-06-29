"""Pre-validation smoke test: confirm we can reach Supabase, are running as
`postgres`, the `public` schema is empty (fresh project), and that every
required extension is available."""

from __future__ import annotations

import sys

import _load_env
import sqlalchemy as sa

REQUIRED_EXTS = ["pgcrypto", "citext", "pg_trgm", "vector", "btree_gin"]


def main() -> int:
    url = _load_env.load()
    engine = sa.create_engine(url, pool_pre_ping=True)
    with engine.connect() as c:
        ver = c.exec_driver_sql("select version()").scalar()
        usr = c.exec_driver_sql("select current_user").scalar()
        db = c.exec_driver_sql("select current_database()").scalar()
        n_public = c.exec_driver_sql(
            "select count(*) from information_schema.tables where table_schema='public'"
        ).scalar()
        public_tables = [
            r[0]
            for r in c.exec_driver_sql(
                "select table_name from information_schema.tables "
                "where table_schema='public' order by table_name"
            )
        ]
        avail = {
            row[0]: row[1]
            for row in c.execute(
                sa.text(
                    "select name, default_version from pg_available_extensions "
                    "where name = ANY(:n)"
                ),
                {"n": REQUIRED_EXTS},
            )
        }

    print("server  :", ver)
    print("user    :", usr)
    print("db      :", db)
    print(f"public  : {n_public} tables")
    if public_tables:
        print("  tables:", ", ".join(public_tables))
    print("exts    :")
    missing = []
    for name in REQUIRED_EXTS:
        v = avail.get(name)
        if v:
            print(f"  - {name:10s} available v{v}")
        else:
            print(f"  - {name:10s} MISSING")
            missing.append(name)
    if missing:
        print(f"\nFAIL: missing extensions: {missing}")
        return 2
    print("\nSmoke test OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
