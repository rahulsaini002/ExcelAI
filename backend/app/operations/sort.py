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
    and plain text alphabetically but case-insensitively. Applied per sort column
    by pandas' sort_values(key=...). Blanks stay blank so they sort to the end.
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
    return series.astype("string").str.lower()  # case-insensitive text (keeps <NA>)


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
    # Default any unspecified order to ascending.
    ascending = [(orders[i] if i < len(orders) else "asc") != "desc" for i in range(len(columns))]

    # Blanks always sorted to the end, regardless of direction.
    df = df.sort_values(
        by=columns, ascending=ascending, kind="stable", na_position="last", key=_sort_key
    ).reset_index(drop=True)

    parts = [f"{c} {'descending' if not asc else 'ascending'}" for c, asc in zip(columns, ascending)]
    return df, f"Sorted by {', '.join(parts)}."
