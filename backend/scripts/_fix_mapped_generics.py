"""One-shot codemod: parameterise bare `Mapped[dict]` / `Mapped[list]`.

mypy --strict requires every generic type to carry its parameters. The ORM
models were authored with the SQLAlchemy 2.0 typed-Mapped pattern but used
unparameterised `dict` / `list` for JSONB and ARRAY columns. This script
rewrites them to `dict[str, Any]` and `list[Any]` respectively, and
ensures `Any` is in the `typing` import line.

The script is idempotent and lives under ``scripts/`` so it can be re-run
if the codebase regresses; it's not part of the CI gate.
"""

from __future__ import annotations

import re
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent.parent / "app" / "infrastructure" / "db" / "models"

# Match both `Mapped[dict]` and `Mapped[dict | None]` (nullable JSONB).
# The negative-lookahead on `[` avoids touching already-parameterised forms
# like `Mapped[dict[str, Any]]`.
_DICT_RE = re.compile(r"\bMapped\[dict(?!\[)(\s*\|\s*None)?\]")
_LIST_RE = re.compile(r"\bMapped\[list(?!\[)(\s*\|\s*None)?\]")
_TYPING_IMPORT_RE = re.compile(r"^from typing import (.+)$", re.MULTILINE)


def ensure_any_import(source: str) -> str:
    if "Any" in source.split("\n", 80)[0:80].__str__():
        # Already present in the prelude — quick path.
        for line in source.splitlines()[:80]:
            if line.strip().startswith("from typing import") and "Any" in line:
                return source

    m = _TYPING_IMPORT_RE.search(source)
    if m:
        existing = [name.strip() for name in m.group(1).split(",")]
        if "Any" in existing:
            return source
        new_names = sorted({*existing, "Any"})
        new_line = f"from typing import {', '.join(new_names)}"
        return source.replace(m.group(0), new_line, 1)

    # No `from typing import ...` line yet — add one after the
    # `from __future__ import annotations` (or top of file).
    future_idx = source.find("from __future__ import annotations")
    if future_idx != -1:
        line_end = source.find("\n", future_idx)
        return source[: line_end + 1] + "\nfrom typing import Any\n" + source[line_end + 1 :]
    return "from typing import Any\n" + source


def _dict_repl(m: re.Match[str]) -> str:
    return "Mapped[dict[str, Any] | None]" if m.group(1) else "Mapped[dict[str, Any]]"


def _list_repl(m: re.Match[str]) -> str:
    return "Mapped[list[Any] | None]" if m.group(1) else "Mapped[list[Any]]"


def transform(source: str) -> tuple[str, int, int]:
    src2, n_dict = _DICT_RE.subn(_dict_repl, source)
    src3, n_list = _LIST_RE.subn(_list_repl, src2)
    if n_dict + n_list:
        src3 = ensure_any_import(src3)
    return src3, n_dict, n_list


def main() -> int:
    total_dict = 0
    total_list = 0
    touched: list[Path] = []
    for py in sorted(MODELS_DIR.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        new_text, n_dict, n_list = transform(text)
        if new_text != text:
            py.write_text(new_text, encoding="utf-8")
            total_dict += n_dict
            total_list += n_list
            touched.append(py)
            print(f"  {py.name}: dict={n_dict} list={n_list}")
    print(
        f"\nRewrote {len(touched)} files: {total_dict} Mapped[dict] -> "
        f"Mapped[dict[str, Any]], {total_list} Mapped[list] -> Mapped[list[Any]]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
