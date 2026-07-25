"""Regression guard: no unquoted reserved SQL identifier in any migration.

α8.5e post-mortem — migration 0011 defined ``provider_quota_state.window``
unquoted, and ``WINDOW`` is a PostgreSQL *reserved* keyword, so ``alembic
upgrade head`` failed with ``syntax error at or near "window"``. That class of
bug only surfaced in the live-DB CI stages. This offline test catches it at
stage 4 (pytest -m unit) with a precise message, before any database is touched.

The check is deliberately conservative: it scans every ``CREATE TABLE`` in the
Alembic migrations and flags a reserved keyword used as an **unquoted** table
name, column name, or constraint-column reference. Reserved identifiers that are
intentionally double-quoted (e.g. ``"window"``) are allowed — quoting is the
sanctioned escape hatch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

VERSIONS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "versions"

# PostgreSQL "reserved" key words (Appendix C): these CANNOT be used as a table
# or column name without double-quoting. Non-reserved words (name, value, key,
# type, timestamp, data, …) are intentionally excluded — they are legal
# identifiers unquoted and must not trip this guard.
RESERVED: frozenset[str] = frozenset(
    {
        "ALL",
        "ANALYSE",
        "ANALYZE",
        "AND",
        "ANY",
        "ARRAY",
        "AS",
        "ASC",
        "ASYMMETRIC",
        "BOTH",
        "CASE",
        "CAST",
        "CHECK",
        "COLLATE",
        "COLUMN",
        "CONSTRAINT",
        "CREATE",
        "CURRENT_CATALOG",
        "CURRENT_DATE",
        "CURRENT_ROLE",
        "CURRENT_TIME",
        "CURRENT_TIMESTAMP",
        "CURRENT_USER",
        "DEFAULT",
        "DEFERRABLE",
        "DESC",
        "DISTINCT",
        "DO",
        "ELSE",
        "END",
        "EXCEPT",
        "FALSE",
        "FETCH",
        "FOR",
        "FOREIGN",
        "FROM",
        "GRANT",
        "GROUP",
        "HAVING",
        "IN",
        "INITIALLY",
        "INTERSECT",
        "INTO",
        "LATERAL",
        "LEADING",
        "LIMIT",
        "LOCALTIME",
        "LOCALTIMESTAMP",
        "NOT",
        "NULL",
        "OFFSET",
        "ON",
        "ONLY",
        "OR",
        "ORDER",
        "PLACING",
        "PRIMARY",
        "REFERENCES",
        "RETURNING",
        "SELECT",
        "SESSION_USER",
        "SOME",
        "SYMMETRIC",
        "SYSTEM_USER",
        "TABLE",
        "THEN",
        "TO",
        "TRAILING",
        "TRUE",
        "UNION",
        "UNIQUE",
        "USER",
        "USING",
        "VARIADIC",
        "WHEN",
        "WHERE",
        "WINDOW",
        "WITH",
    }
)

# Segment leaders that introduce a *table constraint*, not a column definition.
_CONSTRAINT_LEADERS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "EXCLUDE", "LIKE"}
# Constraint column lists whose contents are identifiers that must also be quoted.
_CONSTRAINT_COLS_RE = re.compile(
    r"(?:PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY)\s*\(([^)]*)\)", re.IGNORECASE
)
_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?("?[\w.]+"?)\s*\(', re.IGNORECASE
)


def _is_unquoted_reserved(token: str) -> bool:
    token = token.strip()
    if not token or token.startswith('"'):
        return False
    return token.upper() in RESERVED


def _create_table_blocks(sql: str) -> list[tuple[str, str]]:
    """Return ``(table_name, body)`` for every CREATE TABLE, matching balanced parens."""
    blocks: list[tuple[str, str]] = []
    for m in _CREATE_TABLE_RE.finditer(sql):
        name = m.group(1)
        open_paren = m.end() - 1
        depth = 0
        for j in range(open_paren, len(sql)):
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
                if depth == 0:
                    blocks.append((name, sql[open_paren + 1 : j]))
                    break
    return blocks


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas at paren-depth 0."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def find_reserved_identifiers(sql: str) -> list[str]:
    """Return human-readable violations for unquoted reserved identifiers in ``sql``."""
    violations: list[str] = []
    for table, body in _create_table_blocks(sql):
        if _is_unquoted_reserved(table):
            violations.append(f"table name {table!r} is a reserved keyword — quote it or rename")
        for segment in _split_top_level(body):
            leader = segment.split()[0].upper().strip('"')
            if leader in _CONSTRAINT_LEADERS:
                for collist in _CONSTRAINT_COLS_RE.findall(segment):
                    for col in collist.split(","):
                        if _is_unquoted_reserved(col):
                            violations.append(
                                f"{table}: constraint references reserved column "
                                f"{col.strip()!r} unquoted"
                            )
                continue
            column = segment.split()[0]
            if _is_unquoted_reserved(column):
                violations.append(
                    f"{table}: column {column!r} is a reserved keyword — quote it or rename"
                )
    return violations


def test_committed_migrations_have_no_reserved_identifiers() -> None:
    """Every Alembic migration must quote any reserved SQL identifier it uses."""
    migrations = sorted(VERSIONS_DIR.glob("[0-9]*.py"))
    assert migrations, f"no migrations found under {VERSIONS_DIR}"
    all_violations: list[str] = []
    for path in migrations:
        for v in find_reserved_identifiers(path.read_text(encoding="utf-8")):
            all_violations.append(f"{path.name}: {v}")
    assert not all_violations, "reserved SQL identifiers found:\n" + "\n".join(all_violations)


def test_detector_flags_unquoted_reserved_column() -> None:
    sql = "CREATE TABLE t (id text PRIMARY KEY, window quota_window NOT NULL)"
    violations = find_reserved_identifiers(sql)
    assert any("window" in v for v in violations)


def test_detector_flags_reserved_column_in_primary_key() -> None:
    sql = 'CREATE TABLE t (a text, "window" text, CONSTRAINT pk PRIMARY KEY (a, window))'
    violations = find_reserved_identifiers(sql)
    assert any("constraint references reserved column 'window'" in v for v in violations)


def test_detector_allows_quoted_reserved_identifiers() -> None:
    sql = (
        'CREATE TABLE t (id text, "window" quota_window, CONSTRAINT pk PRIMARY KEY (id, "window"))'
    )
    assert find_reserved_identifiers(sql) == []


def test_detector_ignores_non_reserved_and_substring_columns() -> None:
    # `context_window` contains "window" as a substring but is a legal identifier;
    # `name`, `value`, `key` are non-reserved.
    sql = "CREATE TABLE t (name text, value int, key text, context_window int)"
    assert find_reserved_identifiers(sql) == []
