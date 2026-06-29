from __future__ import annotations

import _load_env
import sqlalchemy as sa

e = sa.create_engine(_load_env.load())
with e.connect() as c:
    rows = c.execute(
        sa.text(
            "select table_name from information_schema.tables "
            "where table_schema='public' and table_type='BASE TABLE' "
            "order by table_name"
        )
    ).all()
print(f"{len(rows)} tables in public:")
for (n,) in rows:
    print(" ", n)
