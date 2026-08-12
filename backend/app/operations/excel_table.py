"""Engine Phase 1.7 — format the data as a NATIVE Excel Table (Area 6).

One op -> a real openpyxl Table object in the saved file: banded rows, header filter
buttons, a chosen style, and an optional Total Row whose per-column aggregations are
live =SUBTOTAL() formulas (they respect Excel's own filtering).

  style        blue | green | orange | grey | yellow | dark (or a raw TableStyle name)
  totals       true -> numeric columns get SUM, the first text column gets a "Total"
               label; or give totals_spec [{column, agg}] for explicit control
  table_name   optional display name (sanitized; must be unique in the workbook)
"""
from __future__ import annotations

import re

import pandas as pd

from .base import OperationError

# Friendly color -> Excel's built-in medium table styles (the accent series).
STYLES = {
    "blue": "TableStyleMedium2",
    "orange": "TableStyleMedium3",
    "grey": "TableStyleMedium4",
    "gray": "TableStyleMedium4",
    "yellow": "TableStyleMedium5",
    "green": "TableStyleMedium7",
    "dark": "TableStyleDark1",
}

# agg name -> SUBTOTAL function code (the *_109 family ignores hidden/filtered rows).
AGGS = {
    "sum": ("sum", 109),
    "average": ("average", 101),
    "avg": ("average", 101),
    "mean": ("average", 101),
    "count": ("count", 103),
    "min": ("min", 105),
    "max": ("max", 104),
}


def excel_table(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict]:
    if len(df) == 0:
        raise OperationError(
            "There are no data rows to put in a table — an Excel table needs at least "
            "one row under the headers."
        )
    style_in = (op.get("table_style") or op.get("style") or "blue")
    style_in = str(style_in).strip()
    if re.fullmatch(r"TableStyle(Light|Medium|Dark)\d{1,2}", style_in):
        style = style_in
    else:
        style = STYLES.get(style_in.lower())
        if style is None:
            raise OperationError(
                f"I don't have a table style called '{style_in}' — try "
                f"{', '.join(sorted(set(STYLES) - {'gray'}))}."
            )

    # Resolve the totals plan: {column -> (agg_name, subtotal_code)}.
    totals: dict[str, tuple[str, int]] = {}
    spec = op.get("totals_spec") or []
    if spec:
        for item in spec:
            col = (item.get("column") or "").strip()
            agg = (item.get("agg") or "sum").strip().lower()
            if col not in df.columns:
                raise OperationError(f"I couldn't find the column '{col}' for the totals row.")
            if agg not in AGGS:
                raise OperationError(
                    f"I can't total with '{agg}' — use {', '.join(sorted(set(a for a, _ in AGGS.values())))}."
                )
            totals[col] = AGGS[agg]
    elif op.get("totals"):
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]) or pd.to_numeric(df[col], errors="coerce").notna().mean() > 0.8:
                totals[col] = AGGS["sum"]
        if not totals:
            raise OperationError(
                "A totals row needs at least one numeric column to total — this sheet "
                "has none I can sum."
            )

    name = (op.get("table_name") or op.get("name") or "SumioTable").strip()
    name = re.sub(r"[^A-Za-z0-9_]", "_", name) or "SumioTable"
    if name[0].isdigit():
        name = "T_" + name

    directive = {
        "type": "table", "style": style, "name": name,
        "totals": {c: {"agg": a, "code": code} for c, (a, code) in totals.items()},
    }

    bits = [f"'{style}' style", "filter buttons", "banded rows"]
    if totals:
        pretty = ", ".join(f"{a} of {c}" for c, (a, _) in totals.items())
        bits.append(f"a totals row ({pretty})")
    note = (f"Formatted the data as an Excel table '{name}' with {', '.join(bits)}. "
            "The table (and its totals) stay live as you edit in Excel.")
    return df, note, directive
