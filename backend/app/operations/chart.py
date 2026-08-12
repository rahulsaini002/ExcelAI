"""Add a chart to the output file (a real Excel chart object, rendered on save).

A chart does NOT change the data — like format/highlight, this validates the request
and returns a render DIRECTIVE that the serializer turns into an openpyxl chart
referencing the sheet's cells. Because it references cells (not a snapshot), the chart
always reflects the CURRENT data, even after earlier steps (filter, sort, …).

Phase 2.4 covers the full openpyxl chart family:
  category charts (labels on the x-axis): bar, line, area, pie, doughnut, radar, stock
  XY charts (a numeric x-axis):           scatter, bubble
Types openpyxl can't create (sparklines, treemap/sunburst, histogram, waterfall,
funnel, map, gauge, heatmap) are DECLINED with the nearest supported alternative —
never silently swapped.
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError, require_columns

# Charts with categorical labels on the x-axis (any x column; numeric y series).
CATEGORY_TYPES = ("bar", "line", "area", "pie", "doughnut", "radar", "stock")
# Charts plotting numbers against numbers (x MUST be numeric).
XY_TYPES = ("scatter", "bubble")
SUPPORTED = CATEGORY_TYPES + XY_TYPES

# Common synonyms → a supported type.
SYNONYMS = {"column": "bar", "donut": "doughnut", "xy": "scatter", "spider": "radar"}

# Types we can't render, each with an honest nearest-alternative suggestion.
FALLBACKS = {
    "histogram": "bar chart of binned counts (bin the values first, then chart)",
    "waterfall": "bar chart",
    "funnel": "bar chart (sorted), which reads the same way",
    "treemap": "pie or bar chart",
    "sunburst": "pie chart (or a grouped bar chart for the levels)",
    "sparkline": "line chart",
    "gauge": "a KPI tile on a dashboard",
    "heatmap": "a conditional-formatting colour scale on the cells",
    "map": "",  # no spreadsheet-native equivalent
    "boxplot": "describe (summary statistics) — box plots aren't available",
    "box": "describe (summary statistics) — box plots aren't available",
    "candlestick": "stock chart",
}


def _is_numeric(series: pd.Series) -> bool:
    """True if every non-blank value in the column is a number."""
    nonblank = series[series.notna() & (series.astype(str).str.strip() != "")]
    if len(nonblank) == 0:
        return False
    return bool(pd.to_numeric(nonblank, errors="coerce").notna().all())


def chart(df: pd.DataFrame, op: dict) -> tuple[str, dict]:
    """Validate a chart request and return (note, directive). Data is unchanged.

    op fields: chart_type, x_column (labels or numeric x), y_columns (numeric value
    column(s)), size_column (bubble only), chart_title (optional).
    """
    if len(df) == 0:
        raise OperationError("There's no data to chart yet.")

    chart_type = (op.get("chart_type") or "bar").strip().lower()
    chart_type = SYNONYMS.get(chart_type, chart_type)
    if chart_type not in SUPPORTED:
        alt = FALLBACKS.get(chart_type)
        if alt:
            raise OperationError(
                f"I can't make a {chart_type} chart — Excel files can't hold one via my "
                f"toolkit. The closest I can do is a {alt}. Want that instead?"
            )
        if chart_type in FALLBACKS:  # known type, but no spreadsheet equivalent
            raise OperationError(
                f"A {chart_type} isn't something an Excel chart can show. I can do: "
                f"{', '.join(SUPPORTED)}."
            )
        raise OperationError(
            f"I don't know a '{chart_type}' chart — I can do {', '.join(SUPPORTED)}."
        )

    x_column = op.get("x_column")
    if not x_column:
        raise OperationError("A chart needs a column for the x-axis.")
    y_columns = [c for c in (op.get("y_columns") or []) if c]
    if not y_columns:
        raise OperationError("A chart needs at least one value column (y-axis).")

    size_column = op.get("size_column")
    needed = [x_column, *y_columns] + ([size_column] if size_column else [])
    require_columns(df, needed)

    # Value columns must be numeric for every chart type.
    non_numeric = [c for c in y_columns if not _is_numeric(df[c])]
    if non_numeric:
        names = ", ".join(f"'{c}'" for c in non_numeric)
        raise OperationError(
            f"A chart's value column must be numbers, but {names} "
            f"{'are' if len(non_numeric) != 1 else 'is'} not."
        )

    note_extra = ""

    # XY charts: the x-axis is a NUMBER line, so x must be numeric too.
    if chart_type in XY_TYPES:
        if not _is_numeric(df[x_column]):
            raise OperationError(
                f"A {chart_type} chart plots numbers against numbers, so the x-axis "
                f"column '{x_column}' must be numeric (it isn't). For category labels, "
                "use a bar or line chart."
            )
        if chart_type == "bubble":
            if not size_column:
                # Fall back to a second value column as the bubble size, if given.
                if len(y_columns) >= 2:
                    size_column = y_columns[1]
                    y_columns = y_columns[:1]
                    note_extra = f" (using '{size_column}' for the bubble sizes)"
                else:
                    raise OperationError(
                        "A bubble chart needs three numbers per point: x, y, and a size. "
                        "Tell me which column sets the bubble size (size_column)."
                    )
            if not _is_numeric(df[size_column]):
                raise OperationError(f"The bubble-size column '{size_column}' must be numeric.")

    # Single-series category charts show one slice/ring per row.
    if chart_type in ("pie", "doughnut") and len(y_columns) > 1:
        y_columns = y_columns[:1]
        note_extra = f" (a {chart_type} shows one series, so I charted the first value column)"

    if chart_type == "stock" and len(y_columns) < 2:
        note_extra = (" (a stock chart usually needs at least high and low columns; "
                      "with one series it looks like a line)")

    title = (op.get("chart_title") or "").strip() or None
    directive = {
        "type": "chart",
        "chart_type": chart_type,
        "x_column": x_column,
        "y_columns": y_columns,
        "size_column": size_column,
        "title": title,
        "rows": int(len(df)),
    }
    ys = ", ".join(y_columns)
    return f"Added a {chart_type} chart of {ys} by {x_column}.{note_extra}", directive
