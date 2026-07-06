"""Add a chart to the output file (a real Excel chart object, rendered on save).

A chart does NOT change the data — like format/highlight, this validates the request
and returns a render DIRECTIVE that the serializer turns into an openpyxl chart
referencing the sheet's cells. Because it references cells (not a snapshot), the chart
always reflects the CURRENT data, even after earlier steps (filter, sort, …).
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError, require_columns

# Chart types we can render today. An unknown type is explained, not silently changed.
SUPPORTED = ("bar", "line", "pie", "area")


def _is_numeric(series: pd.Series) -> bool:
    """True if every non-blank value in the column is a number."""
    nonblank = series[series.notna() & (series.astype(str).str.strip() != "")]
    if len(nonblank) == 0:
        return False
    return bool(pd.to_numeric(nonblank, errors="coerce").notna().all())


def chart(df: pd.DataFrame, op: dict) -> tuple[str, dict]:
    """Validate a chart request and return a (note, directive). Data is unchanged.

    op fields: chart_type (bar/line/pie/area), x_column (the labels / x-axis),
    y_columns (one or more numeric value columns), chart_title (optional).
    """
    if len(df) == 0:
        raise OperationError("There's no data to chart yet.")

    chart_type = (op.get("chart_type") or "bar").strip().lower()
    if chart_type == "column":  # common synonym for a vertical bar chart
        chart_type = "bar"
    if chart_type not in SUPPORTED:
        raise OperationError(
            f"I can't make a '{chart_type}' chart yet — I can do {', '.join(SUPPORTED)}."
        )

    x_column = op.get("x_column")
    if not x_column:
        raise OperationError("A chart needs a column for the labels (x-axis).")
    y_columns = [c for c in (op.get("y_columns") or []) if c]
    if not y_columns:
        raise OperationError("A chart needs at least one value column (y-axis).")

    require_columns(df, [x_column, *y_columns])

    non_numeric = [c for c in y_columns if not _is_numeric(df[c])]
    if non_numeric:
        names = ", ".join(f"'{c}'" for c in non_numeric)
        raise OperationError(
            f"A chart's value column must be numbers, but {names} "
            f"{'are' if len(non_numeric) != 1 else 'is'} not."
        )

    note_extra = ""
    if chart_type == "pie" and len(y_columns) > 1:
        y_columns = y_columns[:1]  # a pie shows a single series
        note_extra = " (a pie shows one series, so I charted the first value column)"

    title = (op.get("chart_title") or "").strip() or None
    directive = {
        "type": "chart",
        "chart_type": chart_type,
        "x_column": x_column,
        "y_columns": y_columns,
        "title": title,
        "rows": int(len(df)),
    }
    ys = ", ".join(y_columns)
    return f"Added a {chart_type} chart of {ys} by {x_column}.{note_extra}", directive
