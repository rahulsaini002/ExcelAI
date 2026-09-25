"""Build a complete report from ANY uploaded table, with no template and no model call.

WHY THIS EXISTS: Reports asked the user to pick a template first, then tried to force
their file into it. A campus-recruitment shortlist picked "Executive Summary" and got
blank Revenue blocks, empty charts, and a row count labelled "Orders". The user's verdict
was the right one: revenue and the rest should not matter — uploading a file should
produce a report of whatever is in it.

So this reads the data and decides what the report should BE, rather than starting from
what someone hoped it would contain.

TWO PROPERTIES IT WILL NOT TRADE AWAY:

1. DETERMINISTIC — no model call anywhere in here. `/report/compute` depends on the Brain
   to map blocks to columns, so on a spent free-tier quota it fails and the user gets
   nothing at all. Generating a report is a data question, not a language one, so it
   should work when the model is unavailable. (The narrative still comes from
   execsummary, which COMPUTES its findings rather than writing them.)

2. NOTHING IS LABELLED AS SOMETHING IT ISN'T — every figure carries a `basis` saying what
   was measured, and titles come from the data's own column names ("Records by Branches"),
   never from a business vocabulary the file never contained.
"""
from __future__ import annotations

import pandas as pd

from . import execsummary
from .kg import _id_like

# A dimension with one row per value tells you nothing (a bar per student); one with a
# single value tells you nothing either. Between those, a breakdown is informative.
_MIN_GROUPS = 2
_MAX_GROUPS = 40
# Rows shown in the detail table: enough to see the real data, small enough to stay
# readable in a PDF.
_DETAIL_ROWS = 25


def _clean_name(col: object) -> str:
    return str(col).strip()


def is_measure(df: pd.DataFrame, col: object) -> bool:
    """A numeric column worth totalling — not an identifier.

    Summing "Roll No" or "S.NO." produces a real number that means nothing, which is the
    same failure as labelling a row count "Orders". Reuses kg._id_like so this codebase
    has ONE definition of an id-ish column rather than a second opinion.
    """
    if not pd.api.types.is_numeric_dtype(df[col]):
        return False
    if _id_like(col):
        return False
    series = df[col].dropna()
    if series.empty:
        return False
    # A near-unique whole-number column is a serial number in practice, whatever it's called.
    if len(series) > 10 and series.nunique() / len(series) > 0.98:
        try:
            if bool((series % 1 == 0).all()):
                return False
        except TypeError:
            return False
    return True


def choose_dimension(df: pd.DataFrame) -> str | None:
    """The column to break the data down BY — the one that makes the most useful groups.

    Prefers a middling number of groups: 12 branches is a story, 548 names is a list, and
    2 genders is thin. Ties break toward the earlier column, which tends to be the more
    structural one.
    """
    best = None
    best_score = -1.0
    for col in df.columns:
        series = df[col].dropna()
        if series.empty or _id_like(col):
            continue
        # A numeric measure is something to aggregate, not to group by.
        if is_measure(df, col):
            continue
        groups = int(series.nunique())
        if groups < _MIN_GROUPS or groups > _MAX_GROUPS:
            continue
        # Peak usefulness around a handful of groups, decaying toward the cap.
        score = 1.0 - abs(groups - 8) / float(_MAX_GROUPS)
        if score > best_score:
            best = col
            best_score = score
    return _clean_name(best) if best is not None else None


def completeness(df: pd.DataFrame) -> float:
    """Share of cells that actually hold a value, 0..1."""
    if df.empty or not len(df.columns):
        return 1.0
    total = len(df) * len(df.columns)
    if not total:
        return 1.0
    return float(total - int(df.isna().sum().sum())) / float(total)


def breakdown(df: pd.DataFrame, dimension: str, top: int = 10) -> dict:
    """Counts per value of `dimension`, largest first — feeds both the table and the chart."""
    counts = df[dimension].dropna().astype(str).value_counts()
    head = counts.head(top)
    total = int(counts.sum()) or 1
    rows = [
        [str(name), f"{int(n):,}", f"{(int(n) / total) * 100:.1f}%"]
        for name, n in head.items()
    ]
    return {
        "columns": [dimension, "Count", "Share"],
        "rows": rows,
        "series": [float(n) for n in head.tolist()],
        "groups": int(counts.size),
    }


def _fmt(x: float) -> str:
    text = f"{x:,.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def numeric_summary(df: pd.DataFrame, measures: list) -> dict | None:
    """Lowest / median / highest / missing per numeric measure. Described, not judged."""
    if not measures:
        return None
    rows = []
    for col in measures:
        s = pd.to_numeric(df[col], errors="coerce")
        clean = s.dropna()
        if clean.empty:
            continue
        rows.append([
            _clean_name(col),
            _fmt(float(clean.min())),
            _fmt(float(clean.median())),
            _fmt(float(clean.max())),
            f"{int(s.isna().sum()):,}",
        ])
    if not rows:
        return None
    return {"columns": ["Column", "Lowest", "Median", "Highest", "Missing"], "rows": rows}


def detail_rows(df: pd.DataFrame, limit: int = _DETAIL_ROWS) -> dict:
    """The first rows as strings, so the report shows the actual data and not just shapes."""
    head = df.head(limit)
    cols = [_clean_name(c) for c in head.columns]
    rows = [
        ["" if pd.isna(v) else str(v) for v in record]
        for record in head.to_numpy()
    ]
    return {"columns": cols, "rows": rows}


def build(df: pd.DataFrame, source: str = "", sheet: str = "") -> list:
    """The report itself: blocks in the shape the UI already renders.

    Ordered the way someone reads a report — what this is, the shape of it, a breakdown,
    the numbers, then the rows themselves.
    """
    if df is None or not len(df):
        return [{
            "type": "narrative",
            "title": "Summary",
            "text": f"{source or 'This file'} has no data rows to report on.",
        }]

    blocks: list = []
    measures = [c for c in df.columns if is_measure(df, c)]
    dimension = choose_dimension(df)

    # Narrative first. execsummary composes independently-computed findings and OMITS
    # anything it cannot ground — no date column means no trend, rather than an invented one.
    text = ""
    try:
        summary = execsummary.generate(df, title=sheet or source or "Summary")
        text = execsummary.as_text(summary)
    except Exception:
        text = ""
    if text:
        blocks.append({"type": "narrative", "title": "Summary", "text": text})

    blocks.append({"type": "kpi", "title": "Records",
                   "value": f"{len(df):,}", "basis": "count of rows"})
    blocks.append({"type": "kpi", "title": "Columns",
                   "value": str(len(df.columns)), "basis": "fields in this sheet"})
    blocks.append({"type": "kpi", "title": "Complete",
                   "value": f"{completeness(df) * 100:.0f}%",
                   "basis": "cells that hold a value"})

    if dimension:
        b = breakdown(df, dimension)
        blocks.append({"type": "kpi", "title": f"Distinct {dimension}",
                       "value": f"{b['groups']:,}",
                       "basis": f"distinct values in {dimension}"})
        blocks.append({"type": "chart", "title": f"Records by {dimension}",
                       "chartType": "bar", "data": b["series"]})
        blocks.append({"type": "table", "title": f"Breakdown by {dimension}",
                       "columns": b["columns"], "rows": b["rows"]})

    # One headline figure per measure, clearly described. No measure means no KPI —
    # inventing one is exactly how "Orders" happened.
    for col in measures[:3]:
        clean = pd.to_numeric(df[col], errors="coerce").dropna()
        if clean.empty:
            continue
        blocks.append({"type": "kpi", "title": f"Average {_clean_name(col)}",
                       "value": _fmt(float(clean.mean())),
                       "basis": f"average of {_clean_name(col)}"})

    stats = numeric_summary(df, measures)
    if stats:
        blocks.append({"type": "table", "title": "Numeric columns",
                       "columns": stats["columns"], "rows": stats["rows"]})

    detail = detail_rows(df)
    blocks.append({"type": "table",
                   "title": f"First {len(detail['rows'])} rows",
                   "columns": detail["columns"], "rows": detail["rows"]})
    return blocks
