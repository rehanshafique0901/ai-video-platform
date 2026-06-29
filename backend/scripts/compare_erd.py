"""Structural diff between the hand-authored ERD and the live-generated ERD.

`docs/database/ERD.md` is split into multiple Mermaid clusters with rich entity
columns and human-readable relationship labels; the generated ERD is a single
flat block with bare relationships (`A ||--o{ B : column`). A textual diff is
useless. This script extracts the *set of entities* and the *set of
relationships* from both files and reports any deltas.

A relationship is normalized to a tuple ``(parent, child, fk_column)``. The
parent/child orientation matches the Mermaid convention used in both files:
``A ||--o{ B : col``  =>  parent=A, child=B, fk_column=col.

Usage:
    python scripts/compare_erd.py <generated.md> <design.md>

Exit code 0 if both sides match (or the design is a strict superset due to
documented synonym tables); 1 if there is a non-trivial drift.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Relationship lines like:  parent ||--o{ child : column
# (also tolerant of various cardinality glyphs and surrounding whitespace)
_REL_RE = re.compile(
    r"""^\s*
        ([a-zA-Z_][\w]*)                      # parent table
        \s+\|\|--                             # left side cardinality "||--"
        (?:o\{|o\|\|--|o\{|\|\{|o\{|o\}|o\|)? # right glyph (loose)
        \s*([a-zA-Z_][\w]*)                   # child table
        \s*:\s*
        "?([^"\s]+)"?                         # label/column (may be quoted)
        \s*$
    """,
    re.VERBOSE,
)
# Simpler version — capture parent ... child : label, ignore the connector
# Matches:  parent ||--o{ child : label
# label may be unquoted (single token, treated as FK column name) or
# double-quoted multi-word prose (treated as human label).
_REL_LOOSE = re.compile(
    r"""^\s*
        ([a-zA-Z_]\w*)            # parent
        \s+\|\|--\S+\s+           # connector glyphs (e.g. ||--o{, ||--|{)
        ([a-zA-Z_]\w*)            # child
        \s*:\s*
        (?:"([^"]*)"|(\S+))       # label: quoted (any chars) OR bare token
        \s*$
    """,
    re.VERBOSE,
)
# Entity declaration lines (open brace):   tablename {
_ENT_OPEN = re.compile(r"^\s*([a-zA-Z_]\w*)\s*\{\s*$")
# Empty entity declaration:  tablename { }
_ENT_EMPTY = re.compile(r"^\s*([a-zA-Z_]\w*)\s*\{\s*\}\s*$")

# Lines after "Cross-cluster references" or similar prose sections are not
# strict edges; we still parse them so the design's "summary" section can
# contribute.

# Tables documented in design but intentionally not created at Step B (these
# are listed under "Deferred Tables" in schema.md).
DEFERRED_DESIGN_ENTITIES: set[str] = set()


def parse_erd(path: Path) -> tuple[set[str], set[tuple[str, str, str]]]:
    entities: set[str] = set()
    rels: set[tuple[str, str, str]] = set()
    inside_mermaid = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```mermaid"):
            inside_mermaid = True
            continue
        if line.strip() == "```":
            inside_mermaid = False
            continue
        if not inside_mermaid:
            continue
        m_empty = _ENT_EMPTY.match(line)
        if m_empty:
            entities.add(m_empty.group(1))
            continue
        m_open = _ENT_OPEN.match(line)
        if m_open and "}" not in line:
            entities.add(m_open.group(1))
            continue
        m_rel = _REL_LOOSE.match(line)
        if m_rel:
            parent, child, quoted_label, bare_label = m_rel.groups()
            label = quoted_label if quoted_label is not None else bare_label
            entities.add(parent)
            entities.add(child)
            rels.add((parent, child, label))
    return entities, rels


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    gen_path = Path(argv[1])
    design_path = Path(argv[2])

    gen_entities, gen_rels = parse_erd(gen_path)
    design_entities, design_rels = parse_erd(design_path)

    # Discard ENUM/lookup pseudo-entities that the design diagrams the design
    # may reference inline (e.g. via comments). We only care about the real
    # tables that show up in either file as a relationship endpoint or an
    # entity declaration.

    only_gen = sorted(gen_entities - design_entities)
    only_design = sorted(design_entities - gen_entities - DEFERRED_DESIGN_ENTITIES)

    # Relationship comparison: the design's labels are prose ("owns",
    # "federates", "has refresh tokens") while the generated uses the actual
    # FK column. We therefore compare on the (parent, child) edge alone for
    # the design vs generated comparison, but ALSO emit the rich detail.
    gen_edges = {(p, c) for (p, c, _) in gen_rels}
    design_edges = {(p, c) for (p, c, _) in design_rels}
    only_gen_edges = sorted(gen_edges - design_edges)
    only_design_edges = sorted(design_edges - gen_edges)

    report = {
        "generated_path": str(gen_path),
        "design_path": str(design_path),
        "entity_counts": {
            "generated": len(gen_entities),
            "design": len(design_entities),
            "shared": len(gen_entities & design_entities),
        },
        "edge_counts": {
            "generated": len(gen_edges),
            "design": len(design_edges),
            "shared": len(gen_edges & design_edges),
        },
        "entities_only_in_generated": only_gen,
        "entities_only_in_design": only_design,
        "edges_only_in_generated": [f"{p} -> {c}" for p, c in only_gen_edges],
        "edges_only_in_design": [f"{p} -> {c}" for p, c in only_design_edges],
    }

    # If a 4th arg is given, write the structured report there. Stdout stays
    # human-readable so the same script feeds both CI logs and a JSON artefact.
    if len(argv) >= 4:
        Path(argv[3]).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {Path(argv[3]).resolve()}")

    print("== ERD structural diff ==")
    print(json.dumps(report, indent=2))

    # `edges_only_in_generated` is expected: the design ERD intentionally elides
    # cross-cluster FKs for readability (each cluster diagrams its own edges
    # only). The validator only fails if entity sets disagree OR edges declared
    # in the design are missing from the implementation.
    real_drift = only_gen or only_design or only_design_edges
    print()
    if not real_drift:
        print(
            f"RESULT: no schema drift. "
            f"Entities match ({len(gen_entities)}); "
            f"every design-declared edge is present in the implementation "
            f"({len(design_edges)} edges checked); "
            f"{len(only_gen_edges)} additional FKs exist in the implementation that "
            f"the cluster-split design diagrams omit (expected — see notes)."
        )
        return 0
    print("RESULT: drift detected (see lists above).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
