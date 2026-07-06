"""Advanced reshaping — unpivot (wide→long), pivot (long→wide), transpose.

Each is a normal data transform: validate inputs, reshape with pandas, return the new
DataFrame + a plain-language note. unpivot is the headline ("monthly columns → tidy
rows"); pivot is its inverse (a summary grid); transpose flips rows ↔ columns.
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError, require_columns

# agg_func name → the aggregator pandas.pivot_table understands.
_AGG = {"sum": "sum", "mean": "mean", "average": "mean", "count": "count", "min": "min", "max": "max"}


def unpivot(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Wide → long. Keep `id_columns`, melt `value_columns` into one var/value pair."""
    id_cols = [c for c in (op.get("id_columns") or []) if c]
    value_cols = [c for c in (op.get("value_columns") or []) if c]
    require_columns(df, [*id_cols, *value_cols])
    if not value_cols:
        value_cols = [c for c in df.columns if c not in id_cols]
    if not value_cols:
        raise OperationError("Unpivot needs at least one column to turn into rows.")

    var_name = (op.get("var_name") or "Variable").strip() or "Variable"
    value_name = (op.get("value_name") or "Value").strip() or "Value"
    out = df.melt(
        id_vars=id_cols, value_vars=value_cols, var_name=var_name, value_name=value_name
    )
    note = (
        f"Unpivoted {len(value_cols)} column{'s' if len(value_cols) != 1 else ''} into "
        f"'{var_name}' / '{value_name}' — {len(out)} rows."
    )
    return out, note


def pivot(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Long → wide summary. index_columns × pivot_column, aggregating value_column."""
    index = [c for c in (op.get("index_columns") or []) if c]
    col = op.get("pivot_column")
    val = op.get("value_column")
    if not index:
        raise OperationError("Pivot needs at least one row (index) column.")
    if not col or not val:
        raise OperationError("Pivot needs a column to spread and a value column.")
    require_columns(df, [*index, col, val])

    agg = (op.get("agg_func") or "sum").lower()
    aggfunc = _AGG.get(agg, "sum")

    work = df.copy()
    if aggfunc in ("sum", "mean", "min", "max"):
        # numeric aggregations need numbers — coerce so totals are real, not concatenations
        work[val] = pd.to_numeric(work[val], errors="coerce")

    pt = pd.pivot_table(
        work, index=index, columns=col, values=val, aggfunc=aggfunc, fill_value=0
    )
    pt = pt.reset_index()
    pt.columns = [str(c) for c in pt.columns]  # flatten/stringify for clean headers
    pt.columns.name = None
    note = (
        f"Pivoted into a {len(pt)}×{len(pt.columns)} summary "
        f"({aggfunc} of {val} by {', '.join(index)} × {col})."
    )
    return pt, note


def transpose(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Flip the table: rows become columns and vice-versa. If `header_column` is given,
    its values become the new column headers."""
    header = op.get("header_column")
    if header:
        require_columns(df, [header])
        t = df.set_index(header).T
    else:
        t = df.T
    t.index.name = "Field"  # the former column names become this first column
    t = t.reset_index()
    t.columns = [str(c) for c in t.columns]
    t.columns.name = None
    note = f"Transposed the table — now {len(t)} rows × {len(t.columns)} columns."
    return t, note
