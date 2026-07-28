"""Shared tag normalisation for the Media Library (α9.2 §7.5).

Tags are lower-cased, whitespace-trimmed, de-duplicated (first occurrence wins),
and empty entries dropped — a deterministic canonical form so the GIN-backed
ANY-of browse filter matches predictably regardless of client casing/spacing.
"""

from __future__ import annotations

from collections.abc import Iterable


def normalize_tags(tags: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        cleaned = tag.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return tuple(out)
