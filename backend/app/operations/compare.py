"""Engine Phase 2.9 — workbook compare (Area 16): "what changed between these two files?"

A structured, honest diff of two tables: which COLUMNS were added/removed, which ROWS
were added/removed, and which CELLS changed (old → new). Rows are matched by a key
column when one is given (robust to inserts/reorders); otherwise positionally (row i of
A vs row i of B). Every difference is real — computed by comparing the actual values, so
nothing is invented. The result is a plain "Comparison" table anyone can read.
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError

_MAX_DIFFS = 1000  # a diff longer than this is summarized, not listed cell-by-cell


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or (str(v).strip() == "")


def _cell_equal(x, y) -> bool:
    """Two cells are 'the same' when both are blank, OR equal as numbers (so 10 == '10'),
    OR equal as trimmed text. Keeps 10 vs 10.0 vs '10' from looking like a change."""
    bx, by = _blank(x), _blank(y)
    if bx or by:
        return bx and by
    nx, ny = pd.to_numeric([x], errors="coerce")[0], pd.to_numeric([y], errors="coerce")[0]
    if pd.notna(nx) and pd.notna(ny):
        return float(nx) == float(ny)
    return str(x).strip() == str(y).strip()


def _show(v) -> str:
    return "" if _blank(v) else str(v).strip()


def compare_tables(a: pd.DataFrame, b: pd.DataFrame, a_name: str, b_name: str,
                   key_column: str | None = None) -> tuple[pd.DataFrame, str]:
    diffs: list[tuple[str, str, str, str]] = []  # (Change, Where, Was, Now)

    cols_a, cols_b = list(a.columns), list(b.columns)
    removed_cols = [c for c in cols_a if c not in cols_b]
    added_cols = [c for c in cols_b if c not in cols_a]
    shared_cols = [c for c in cols_a if c in cols_b]
    for c in removed_cols:
        diffs.append(("Column removed", str(c), "in " + a_name, "—"))
    for c in added_cols:
        diffs.append(("Column added", str(c), "—", "in " + b_name))

    changed = added_rows = removed_rows = 0
    key = key_column if (key_column and key_column in cols_a and key_column in cols_b) else None

    if key:
        # Match rows by key (first occurrence). Robust to inserts/reorders.
        def keymap(df):
            m: dict = {}
            for i, k in enumerate(df[key]):
                kk = str(k).strip().lower()
                m.setdefault(kk, i)
            return m
        ma, mb = keymap(a), keymap(b)
        for kk, i in ma.items():
            if kk not in mb:
                removed_rows += 1
                if len(diffs) < _MAX_DIFFS:
                    diffs.append(("Row removed", f"{key}={_show(a.iloc[i][key])}", "in " + a_name, "—"))
        for kk, j in mb.items():
            if kk not in ma:
                added_rows += 1
                if len(diffs) < _MAX_DIFFS:
                    diffs.append(("Row added", f"{key}={_show(b.iloc[j][key])}", "—", "in " + b_name))
        for kk, i in ma.items():
            if kk in mb:
                ra, rb = a.iloc[i], b.iloc[mb[kk]]
                for c in shared_cols:
                    if c == key:
                        continue
                    if not _cell_equal(ra[c], rb[c]):
                        changed += 1
                        if len(diffs) < _MAX_DIFFS:
                            diffs.append(("Cell changed", f"{key}={_show(ra[key])} · {c}",
                                          _show(ra[c]), _show(rb[c])))
    else:
        # Positional: compare row i of A with row i of B.
        n = min(len(a), len(b))
        for i in range(n):
            ra, rb = a.iloc[i], b.iloc[i]
            for c in shared_cols:
                if not _cell_equal(ra[c], rb[c]):
                    changed += 1
                    if len(diffs) < _MAX_DIFFS:
                        diffs.append(("Cell changed", f"row {i + 1} · {c}", _show(ra[c]), _show(rb[c])))
        for i in range(n, len(b)):
            added_rows += 1
            if len(diffs) < _MAX_DIFFS:
                diffs.append(("Row added", f"row {i + 1}", "—", "in " + b_name))
        for i in range(n, len(a)):
            removed_rows += 1
            if len(diffs) < _MAX_DIFFS:
                diffs.append(("Row removed", f"row {i + 1}", "in " + a_name, "—"))

    total = changed + added_rows + removed_rows + len(added_cols) + len(removed_cols)
    if total == 0:
        result = pd.DataFrame([("No differences", "The two files match.", "", "")],
                              columns=["Change", "Where", "Was", "Now"])
        note = (f"Compared '{a_name}' and '{b_name}': they are identical (same columns, "
                "rows, and values).")
        return result, note

    result = pd.DataFrame(diffs, columns=["Change", "Where", "Was", "Now"])
    parts = []
    if changed:
        parts.append(f"{changed} changed cell{'s' if changed != 1 else ''}")
    if added_rows:
        parts.append(f"{added_rows} added row{'s' if added_rows != 1 else ''}")
    if removed_rows:
        parts.append(f"{removed_rows} removed row{'s' if removed_rows != 1 else ''}")
    if added_cols:
        parts.append(f"{len(added_cols)} added column{'s' if len(added_cols) != 1 else ''}")
    if removed_cols:
        parts.append(f"{len(removed_cols)} removed column{'s' if len(removed_cols) != 1 else ''}")
    how = f"matched by '{key}'" if key else "compared row by row"
    note = f"Compared '{a_name}' and '{b_name}' ({how}): " + ", ".join(parts) + "."
    if total > _MAX_DIFFS:
        note += f" Showing the first {_MAX_DIFFS} of {total} differences on the 'Comparison' sheet."
    else:
        note += " See the 'Comparison' sheet for the details."
    return result, note
