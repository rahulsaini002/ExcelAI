"""Sort rows by one or more columns.

Values order by MEANING, not text: numbers (even when stored as text) sort
numerically, dates chronologically, and plain text case-insensitively. Blanks
always sort to the end. This is the reference example for the operations/ pattern.
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError, require_columns, to_datetime


def _sort_key(series: pd.Series) -> pd.Series:
    """Return a sort key so values order by meaning, not text.

    Numbers (even when stored as text) sort numerically, dates chronologically,
    and plain text alphabetically, ignoring case AND surrounding whitespace. Applied
    per sort column by pandas' sort_values(key=...). Blanks stay blank so they sort
    to the end.

    Trimming matters as much as lower-casing: real sheets are full of stray spaces, and
    ' East' / 'East' / 'EAST' look identical to a user. Keying on the raw text scattered
    those across the sheet, which defeats the point of sorting — and disagreed with the
    rest of the engine, where lookups and de-duplication already match keys trimmed and
    case-folded. Only the sort KEY is normalized; the cell values are never altered.
    """
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
        return series
    nonnull = int(series.notna().sum())
    if nonnull:
        nums = pd.to_numeric(series, errors="coerce")
        if int(nums.notna().sum()) == nonnull:  # every value is a number
            return nums
        dates = to_datetime(series)
        if int(dates.notna().sum()) == nonnull:  # every value is a date
            return dates
    # case- and whitespace-insensitive text (keeps <NA> so blanks still sort last)
    return series.astype("string").str.strip().str.lower()


def sort(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Sort `df` by op['columns'] using op['orders'] (asc/desc, one per column).

    Returns a NEW sorted DataFrame (the input is not mutated) and a plain-language
    note. Raises OperationError if no column is given or a column is missing.
    """
    columns = op.get("columns") or []
    if not columns:
        raise OperationError("Sort needs at least one column.")
    require_columns(df, columns)

    orders = op.get("orders") or []
    # Default any unspecified order to ascending. Orders arrive as free text now (the
    # schema enum was dropped for serving-size reasons) — normalize "desc"/"descending"
    # etc., and refuse anything unrecognizable rather than silently sorting ascending.
    ascending: list[bool] = []
    for i in range(len(columns)):
        o = str(orders[i] if i < len(orders) else "asc").strip().lower()
        if o.startswith("desc"):
            ascending.append(False)
        elif o.startswith("asc") or o == "":
            ascending.append(True)
        else:
            raise OperationError(
                f"I don't understand the sort order '{o}' — use ascending or descending."
            )

    # Blanks always sorted to the end, regardless of direction.
    df = df.sort_values(
        by=columns, ascending=ascending, kind="stable", na_position="last", key=_sort_key
    ).reset_index(drop=True)

    parts = [f"{c} {'descending' if not asc else 'ascending'}" for c, asc in zip(columns, ascending)]
    return df, f"Sorted by {', '.join(parts)}."
