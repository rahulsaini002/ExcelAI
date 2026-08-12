"""Engine Phase 1.9 — explain changes as CELL NOTES (Area 12, ties to Explainable AI).

The `explain_changes` op (usually the LAST step of a plan) diffs the plan's ORIGINAL
table against the final one and turns the differences into openpyxl comments:

  * cell-level notes ("Sumio: was blank → 0") — only when the row COUNT is unchanged,
    because row-removing ops reset the index and positional alignment would lie;
  * an A1 summary note whenever rows were added/removed (with the ops' own honest
    notes: "Removed 20 duplicate rows…");
  * a header note on every column the plan added.

The data itself is never altered — comments only.
"""
from __future__ import annotations

import pandas as pd

MAX_CELL_NOTES = 300


def _short(v) -> str:
    try:
        if v is None or pd.isna(v):
            return "blank"
    except (TypeError, ValueError):
        pass
    s = str(v)
    return f"'{s[:40]}…'" if len(s) > 42 else f"'{s}'"


def explain_changes(
    original: pd.DataFrame | None,
    current: pd.DataFrame,
    prior_notes: list[str],
) -> tuple[str, dict | None]:
    """Returns (note, comments-directive|None)."""
    if original is None:
        return ("I couldn't find the original state to compare against, so no cell "
                "notes were added."), None
    if not prior_notes:
        return ("Nothing ran before this step, so there are no changes to explain — "
                "add it after the operations you want annotated."), None

    cells: list[dict] = []
    summary_bits: list[str] = list(prior_notes)

    # Columns the plan added -> a note on their header cell.
    added_cols = [c for c in current.columns if c not in original.columns]
    for c in added_cols:
        why = next((n for n in prior_notes if f"'{c}'" in n), "Added by Sumio.")
        cells.append({"row": 1, "column": str(c), "text": why[:250]})

    rows_delta = len(current) - len(original)
    truncated = 0
    if rows_delta == 0:
        # Same shape: positional alignment is safe -> per-cell notes.
        common = [c for c in current.columns if c in original.columns]
        for col in common:
            o = original[col].reset_index(drop=True)
            n = current[col].reset_index(drop=True)
            both_blank = o.isna() & n.isna()
            same = (o == n) | both_blank
            # object-dtype NaN comparisons: treat str-equal as same too
            diff_idx = [i for i in range(len(n)) if not bool(same.iloc[i])]
            for i in diff_idx:
                if len(cells) >= MAX_CELL_NOTES:
                    truncated += 1
                    continue
                cells.append({
                    "row": i + 2,  # sheet row (1 = header)
                    "column": str(col),
                    "text": f"Was {_short(o.iloc[i])} → now {_short(n.iloc[i])}."[:250],
                })
    else:
        verb = "removed" if rows_delta < 0 else "added"
        summary_bits.insert(0, f"{abs(rows_delta):,} row{'s' if abs(rows_delta) != 1 else ''} {verb}")

    if not cells and rows_delta == 0:
        return "Nothing changed, so there was nothing to annotate.", None

    directive = {
        "type": "comments",
        "cells": cells,
        "summary": " · ".join(summary_bits)[:900] if rows_delta != 0 else None,
    }

    n_cells = len(cells)
    parts = []
    if n_cells:
        parts.append(f"{n_cells:,} cell note{'s' if n_cells != 1 else ''}")
    if truncated:
        parts.append(f"({truncated:,} more changes not annotated — too many for notes)")
    if directive["summary"]:
        parts.append("a summary note on A1")
    note = ("Explained the changes: " + ", ".join(parts) +
            ". Hover a marked cell in Excel to read its note; the data is unchanged.")
    if rows_delta != 0:
        note += (" (Rows were added/removed, so per-cell notes are skipped — positions "
                 "shift and notes could land on the wrong cells; the A1 note carries "
                 "the full story.)")
    return note, directive
