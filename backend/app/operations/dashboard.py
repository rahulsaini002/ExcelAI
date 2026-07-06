"""Assemble a one-page dashboard — KPIs + charts + a short written summary — onto a
new 'Dashboard' sheet in the output file.

Like chart/format, this does NOT change the data: it validates the request, computes
the KPI numbers from the CURRENT data, and returns a render directive that the
serializer turns into a laid-out Dashboard sheet (charts reference the data cells, so
the whole thing regenerates cleanly whenever the data changes).
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError, require_columns

SUPPORTED_CHART = ("bar", "line", "pie", "area")


def _is_numeric(series: pd.Series) -> bool:
    nonblank = series[series.notna() & (series.astype(str).str.strip() != "")]
    if len(nonblank) == 0:
        return False
    return bool(pd.to_numeric(nonblank, errors="coerce").notna().all())


def _abbrev(x: float) -> str:
    ax = abs(x)
    if ax >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if ax >= 1_000:
        return f"{x / 1_000:.1f}K"
    return f"{int(x)}" if x == int(x) else f"{x:.2f}"


def _format_number(x: float, fmt: str | None) -> str:
    if fmt == "percent":
        return f"{x:.1f}%"
    if fmt == "currency":
        return "₹" + _abbrev(x)
    if abs(x) >= 10_000:
        return _abbrev(x)
    return f"{int(x):,}" if x == int(x) else f"{x:,.2f}"


def _compute_kpi(df: pd.DataFrame, agg: str, column: str | None, fmt: str | None) -> str | None:
    """Compute one KPI value from the current data, formatted for display."""
    try:
        if agg == "count":
            return _format_number(float(len(df)), fmt or "number")
        if not column or column not in df.columns:
            return None
        if agg == "count_distinct":
            return _format_number(float(df[column].nunique()), fmt or "number")
        s = pd.to_numeric(df[column], errors="coerce").dropna()
        if s.empty:
            return None
        fn = {"sum": s.sum, "mean": s.mean, "average": s.mean, "min": s.min, "max": s.max}.get(agg)
        if fn is None:
            return None
        return _format_number(float(fn()), fmt)
    except Exception:
        return None


def dashboard(df: pd.DataFrame, op: dict) -> tuple[str, dict]:
    """Validate a dashboard request and return (note, directive). Data is unchanged.

    op fields:
      dashboard_title: optional title for the sheet
      kpis:    list of {label, agg, column?, format?}
      charts:  list of {chart_type, x_column, y_columns, title?}
      summary: optional short written summary
    """
    if len(df) == 0:
        raise OperationError("There's no data to build a dashboard from yet.")

    # KPIs — compute each value now from the current data.
    kpis_out: list[dict] = []
    for k in op.get("kpis") or []:
        label = (k.get("label") or "").strip() or "Metric"
        column = k.get("column")
        if column and column not in df.columns:
            require_columns(df, [column])  # raises a friendly "I don't see the column…"
        value = _compute_kpi(df, k.get("agg"), column, k.get("format"))
        kpis_out.append({"label": label, "value": value if value is not None else "—"})

    # Charts — validate each (same rules as the chart operation).
    charts_out: list[dict] = []
    for c in op.get("charts") or []:
        ct = (c.get("chart_type") or "bar").strip().lower()
        if ct == "column":
            ct = "bar"
        if ct not in SUPPORTED_CHART:
            raise OperationError(
                f"I can't make a '{ct}' chart yet — I can do {', '.join(SUPPORTED_CHART)}."
            )
        x = c.get("x_column")
        ys = [y for y in (c.get("y_columns") or []) if y]
        if not x or not ys:
            raise OperationError("Each dashboard chart needs an x column and a value column.")
        require_columns(df, [x, *ys])
        non_numeric = [y for y in ys if not _is_numeric(df[y])]
        if non_numeric:
            names = ", ".join(f"'{y}'" for y in non_numeric)
            raise OperationError(f"A chart's value column must be numbers, but {names} is not.")
        if ct == "pie":
            ys = ys[:1]
        charts_out.append({
            "chart_type": ct, "x_column": x, "y_columns": ys,
            "title": (c.get("title") or "").strip() or None,
        })

    if not kpis_out and not charts_out:
        raise OperationError("A dashboard needs at least one KPI or chart.")

    directive = {
        "type": "dashboard",
        "title": (op.get("dashboard_title") or "").strip() or "Dashboard",
        "kpis": kpis_out,
        "charts": charts_out,
        "summary": (op.get("summary") or "").strip(),
        "rows": int(len(df)),
    }
    note = (
        f"Built a dashboard with {len(kpis_out)} KPI"
        f"{'' if len(kpis_out) == 1 else 's'} and {len(charts_out)} chart"
        f"{'' if len(charts_out) == 1 else 's'} on a new 'Dashboard' sheet."
    )
    return note, directive
