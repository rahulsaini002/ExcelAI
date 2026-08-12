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
# The user-facing verb for each aggregator (so notes/errors read naturally).
_PRETTY = {"sum": "sum", "mean": "average", "count": "count", "min": "min", "max": "max"}


def _blank_mask(series: pd.Series) -> pd.Series:
    """True where a cell is empty (NaN, or a string that's only whitespace)."""
    return series.isna() | (series.astype(str).str.strip() == "")


def unpivot(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Wide → long. Keep `id_columns`, melt `value_columns` into one var/value pair."""
    id_cols = [c for c in (op.get("id_columns") or []) if c]
    value_cols = [c for c in (op.get("value_columns") or []) if c]
    require_columns(df, [*id_cols, *value_cols])
    # A column can't be both kept and melted — pandas raises a cryptic error, so catch
    # it here with a plain-language message.
    overlap = [c for c in value_cols if c in id_cols]
    if overlap:
        raise OperationError(
            f"The column{'s' if len(overlap) != 1 else ''} {', '.join(overlap)} "
            f"can't be both kept and turned into rows — pick one role for it."
        )
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
    if len(df) == 0:
        raise OperationError("There are no data rows to pivot.")
    index = [c for c in (op.get("index_columns") or []) if c]
    col = op.get("pivot_column")
    val = op.get("value_column")
    if not index:
        raise OperationError("Pivot needs at least one row (index) column.")
    if not col or not val:
        raise OperationError("Pivot needs a column to spread and a value column.")
    require_columns(df, [*index, col, val])
    if val in index or val == col:
        raise OperationError(
            f"'{val}' can't be both a layout field and the value being aggregated."
        )

    agg = (op.get("agg_func") or "sum").lower()
    aggfunc = _AGG.get(agg)
    if aggfunc is None:  # never silently default — a wrong-but-confident sum is worse
        raise OperationError(
            f"I can't pivot with '{agg}' — use sum, average, count, min, or max."
        )

    work = df.copy()
    blanks_ignored = bad_num = 0
    if aggfunc in ("sum", "mean", "min", "max"):
        # numeric aggregations need numbers — coerce so totals are real, not concatenations
        raw = work[val]
        nums = pd.to_numeric(raw.astype(object), errors="coerce")
        blanks_ignored = int(_blank_mask(raw).sum())
        bad_num = int((nums.isna() & ~_blank_mask(raw)).sum())
        if nums.notna().sum() == 0:
            raise OperationError(
                f"Can't {_PRETTY[aggfunc]} '{val}' — it looks like text, not numbers."
            )
        work[val] = nums

    # Missing (row × column) combinations: 0 is the honest fill for a SUM or a COUNT,
    # but for average/min/max a missing combination has NO value — filling it with 0
    # would fabricate data, so those stay blank.
    fill = 0 if aggfunc in ("sum", "count") else None
    pt = pd.pivot_table(
        work, index=index, columns=col, values=val, aggfunc=aggfunc, fill_value=fill
    )
    if isinstance(pt, pd.Series):
        pt = pt.to_frame()
    pt = pt.reset_index()
    pt.columns = [str(c) for c in pt.columns]  # flatten/stringify for clean headers
    pt.columns.name = None
    note = (
        f"Pivoted into a {len(pt)}×{len(pt.columns)} summary "
        f"({_PRETTY[aggfunc]} of {val} by {', '.join(index)} × {col})."
    )
    ignored = []
    if blanks_ignored:
        ignored.append(f"{blanks_ignored} blank cell{'s' if blanks_ignored != 1 else ''}")
    if bad_num:
        ignored.append(f"{bad_num} non-numeric cell{'s' if bad_num != 1 else ''}")
    if ignored:
        note += f" (ignored {' and '.join(ignored)} in '{val}')"
    if aggfunc not in ("sum", "count"):
        note += " Blank cells are combinations that don't occur in the data."
    return pt, note


def transpose(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Flip the table: rows become columns and vice-versa. If `header_column` is given,
    its values become the new column headers."""
    if len(df) == 0:
        raise OperationError("There are no data rows to transpose.")
    header = op.get("header_column")
    if header:
        require_columns(df, [header])
        # The header column's values BECOME the new column names, so they must be
        # unique — duplicates would collapse/overwrite columns and silently lose data.
        keys = df[header].astype(str)
        dupes = keys[keys.duplicated()].unique().tolist()
        if dupes:
            shown = ", ".join(f"'{d}'" for d in dupes[:3]) + ("…" if len(dupes) > 3 else "")
            raise OperationError(
                f"Can't use '{header}' as the new headers — it has repeated values "
                f"({shown}). New column names must be unique. Remove duplicates first, "
                f"or transpose without a header column."
            )
        t = df.set_index(header).T
    else:
        t = df.T
    t.index.name = "Field"  # the former column names become this first column
    t = t.reset_index()
    t.columns = [str(c) for c in t.columns]
    t.columns.name = None
    note = f"Transposed the table — now {len(t)} rows × {len(t.columns)} columns."
    return t, note
