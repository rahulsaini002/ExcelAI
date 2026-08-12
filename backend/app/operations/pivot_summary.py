"""Engine Phase 2.1 — pivot summaries (Area 8): grouped cross-tab summary tables.

One op covers the PRD's pivot-table need two ways:

  static (default)  trusted pandas computes the grid — works in EVERY Excel version,
                    Google Sheets, and CSV output.
  live              the saved file gets a live =GROUPBY()/=PIVOTBY() spill formula
                    (source data on its own sheet) — needs Microsoft 365, and the
                    note says so. The preview always shows the computed values.

Feature set: 1-D (rows only) and 2-D (rows × one column field) summaries, grand
totals computed FROM THE RAW DATA (so a "Total" under averages is the true overall
average, not an average of averages), % of total (grand / row / column — sum and
count only), and date bucketing (day/week/month/quarter/year) for messy date
columns, including dates stored as text.

Native interactive PivotTable OBJECTS (drag-and-drop field lists, slicers) are
deliberately NOT created — openpyxl cannot write them (program Stage 6 deferral).
This computed grid / live-formula pair is Sumio's honest replacement.
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

from .base import OperationError, require_columns, to_datetime

_FUNCS = {"sum": "sum", "mean": "mean", "average": "mean", "avg": "mean",
          "count": "count", "min": "min", "max": "max"}
_PRETTY = {"sum": "sum", "mean": "average", "count": "count", "min": "min", "max": "max"}
_EXCEL_FUNC = {"sum": "SUM", "mean": "AVERAGE", "count": "COUNT", "min": "MIN", "max": "MAX"}
_BUCKETS = ("day", "week", "month", "quarter", "year")
_MAX_GRID_CELLS = 200_000


def _blank_mask(series: pd.Series) -> pd.Series:
    return series.isna() | (series.astype(str).str.strip() == "")


def _looks_like_dates(series: pd.Series) -> bool:
    """True when a dimension column is genuinely date-like. Datetime dtype counts;
    object columns count when most non-blank values are real date/datetime objects
    or date-looking strings (len >= 6 — so Qty-as-text '23' is never mistaken for a
    date, which dateutil would happily parse)."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if pd.api.types.is_numeric_dtype(series):
        return False
    vals = series[~_blank_mask(series)].head(200)
    if len(vals) == 0:
        return False
    ok = 0
    for v in vals:
        if isinstance(v, (_dt.date, _dt.datetime, pd.Timestamp)):
            ok += 1
        elif isinstance(v, str) and len(v.strip()) >= 6 and pd.notna(to_datetime(v)):
            ok += 1
    return ok / len(vals) >= 0.6


def _bucket_labels(series: pd.Series, bucket: str) -> tuple[pd.Series, int]:
    """Turn a (messy) date column into sortable bucket labels. Blanks become
    '(blank)'; non-blank values that aren't recognizable dates become
    '(not a date)' and are counted so the note can say so honestly."""
    parsed = to_datetime(series)
    blank = _blank_mask(series)
    bad = parsed.isna() & ~blank

    if bucket == "day":
        lab = parsed.dt.strftime("%Y-%m-%d")
    elif bucket == "week":
        iso = parsed.dt.isocalendar()
        lab = iso["year"].astype("string") + "-W" + iso["week"].astype("string").str.zfill(2)
    elif bucket == "month":
        lab = parsed.dt.strftime("%Y-%m")
    elif bucket == "quarter":
        lab = (parsed.dt.year.astype("Int64").astype("string") + "-Q"
               + parsed.dt.quarter.astype("Int64").astype("string"))
    else:  # year
        lab = parsed.dt.year.astype("Int64").astype("string")
    lab = lab.astype(object)
    lab[bad] = "(not a date)"
    lab[blank] = "(blank)"
    return lab, int(bad.sum())


def _dim_labels(series: pd.Series) -> pd.Series:
    """Plain (non-date) dimension labels: values as-is, stringified so mixed-type
    keys sort without crashing; blanks grouped as '(blank)' like aggregate does."""
    lab = series.astype(object).astype(str)
    lab[_blank_mask(series)] = "(blank)"
    return lab


def pivot_summary(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict | None]:
    if len(df) == 0:
        raise OperationError("There are no data rows to summarize.")

    rows = [c for c in (op.get("group_by") or []) if c]
    if not rows:
        raise OperationError(
            "A pivot summary needs at least one field to group rows by — "
            "e.g. 'total Price by Region'."
        )
    colf = op.get("pivot_column") or None
    if colf in rows:
        raise OperationError(
            f"'{colf}' is already a row field — the column field must be a different column."
        )
    val = op.get("value_column") or op.get("agg_column") or None
    if val is not None and (val in rows or val == colf):
        raise OperationError(
            f"'{val}' can't be both a grouping field and the value being summarized."
        )

    raw_func = (op.get("agg_func") or "sum").lower()
    func = _FUNCS.get(raw_func)
    if func is None:
        raise OperationError(
            f"I can't summarize with '{raw_func}' — use sum, average, count, min, or max."
        )
    if val is None and func != "count":
        raise OperationError(
            "Which column should I summarize? e.g. 'sum of Price by Region'."
        )
    require_columns(df, [*rows, *([colf] if colf else []), *([val] if val else [])])

    percent = (op.get("percent_of") or "").lower() or None
    if percent:
        if percent not in ("grand", "row", "column"):
            raise OperationError("percent_of must be 'grand', 'row', or 'column'.")
        if func not in ("sum", "count"):
            raise OperationError(
                f"% of total only makes sense for sum or count — a percent of an "
                f"{_PRETTY[func]} isn't meaningful."
            )
        if not colf and percent in ("row", "column"):
            percent = "grand"  # 1-D: every share is a share of the grand total

    bucket = (op.get("date_bucket") or "").lower() or None
    if bucket and bucket not in _BUCKETS:
        raise OperationError(
            f"I can't group dates by '{bucket}' — use day, week, month, quarter, or year."
        )
    show_totals = op.get("show_totals")
    if show_totals is None:
        show_totals = True
    live = op.get("live")
    if live is None:
        from .. import config
        live = getattr(config, "PIVOT_STYLE", "static") == "live"
    if live and percent:
        raise OperationError(
            "% of total isn't available as a live GROUPBY/PIVOTBY formula — ask for the "
            "percent version without 'live formulas' and I'll compute it as a table."
        )

    # ---- build the working frame: bucketed/plain dimension labels + the value ----
    dims = [*rows, *([colf] if colf else [])]
    note_bits: list[str] = []
    src = pd.DataFrame(index=df.index)
    disp: dict[str, str] = {}  # original field -> display name in the grid
    bucketed_any = False
    for field in dims:
        s = df[field]
        use_bucket = None
        if bucket and _looks_like_dates(s):
            use_bucket = bucket
        elif not bucket and _looks_like_dates(s):
            use_bucket = "month"  # raw timestamps almost never make useful groups
            note_bits.append(
                f"I grouped '{field}' by month — say 'by day', 'by week', 'by quarter', "
                f"or 'by year' to change"
            )
        if use_bucket:
            name = f"{field} ({use_bucket})"
            labels, bad = _bucket_labels(s, use_bucket)
            if bad:
                note_bits.append(
                    f"{bad} cell{'s' if bad != 1 else ''} in '{field}' "
                    f"{'were' if bad != 1 else 'was'}n't a recognizable date — "
                    f"grouped as '(not a date)'"
                )
            if bucket:
                note_bits.append(f"dates in '{field}' grouped by {use_bucket}")
            bucketed_any = True
        else:
            name = field
            labels = _dim_labels(s)
        disp[field] = name
        src[name] = labels.values
    if bucket and not bucketed_any:
        raise OperationError(
            f"You asked to group dates by {bucket}, but none of the chosen fields "
            f"({', '.join(dims)}) look like dates."
        )

    row_disp = [disp[f] for f in rows]
    col_disp = disp[colf] if colf else None

    blanks = bad_num = 0
    if val is not None:
        vals = df[val]
        blanks = int(_blank_mask(vals).sum())
        if func == "count":
            src[val] = vals.mask(_blank_mask(vals)).values
            val_disp, aggfunc = val, "count"
        else:
            nums = pd.to_numeric(vals.astype(object), errors="coerce")
            bad_num = int((nums.isna() & ~_blank_mask(vals)).sum())
            if nums.notna().sum() == 0:
                raise OperationError(
                    f"Can't {_PRETTY[func]} '{val}' — it looks like text, not numbers."
                )
            src[val] = nums.values
            val_disp, aggfunc = val, func
    else:
        src["Count"] = 1
        val_disp, aggfunc = "Count", "sum"  # sum of 1s == row count (margins stay right)

    # ---- size guard: a pivot with a per-row unique key isn't a summary ----
    n_rows = src[row_disp].drop_duplicates().shape[0]
    n_cols = src[col_disp].nunique() if col_disp else 1
    if n_rows * max(n_cols, 1) > _MAX_GRID_CELLS:
        raise OperationError(
            f"That pivot would be about {n_rows:,} rows × {n_cols:,} columns — too big "
            f"to be a useful summary. Pick fields with fewer distinct values, or group "
            f"dates (by month, quarter, …)."
        )

    margins_name = "Total"
    if margins_name in set(map(str, src[row_disp[0]].unique())):
        margins_name = "Grand Total"  # a literal 'Total' group would collide

    fill = 0 if aggfunc in ("sum", "count") else None
    want_margins = show_totals and not percent
    pt = pd.pivot_table(
        src, index=row_disp, columns=col_disp, values=val_disp, aggfunc=aggfunc,
        fill_value=fill, margins=want_margins, margins_name=margins_name,
    )
    if isinstance(pt, pd.Series):
        pt = pt.to_frame()

    # ---- % of total (computed on the raw grid; totals added by hand) ----
    if percent:
        base = pt.astype(float)
        if percent == "grand":
            total = base.values.sum()
            pct = base / total * 100 if total else base * 0.0
            if show_totals:
                if colf:
                    pct[margins_name] = pct.sum(axis=1)
                pct.loc[_total_key(pct, margins_name), :] = pct.sum(axis=0)
        elif percent == "row":
            sums = base.sum(axis=1)
            pct = base.div(sums.where(sums != 0), axis=0).fillna(0) * 100
            if show_totals:
                pct[margins_name] = pct.sum(axis=1)
        else:  # column
            sums = base.sum(axis=0)
            pct = base.div(sums.where(sums != 0), axis=1).fillna(0) * 100
            if show_totals:
                pct.loc[_total_key(pct, margins_name), :] = pct.sum(axis=0)
        pt = pct.round(2)

    # ---- flatten to a plain DataFrame with clean string headers ----
    out = pt.reset_index()
    out.columns = [str(c) for c in out.columns]
    out.columns.name = None
    if not colf:
        # 1-D: name the single value column after what it holds
        value_header = (
            f"%_of_{val}" if percent and val
            else "%_of_rows" if percent
            else f"{_PRETTY[func]}_of_{val}" if val else "count_of_rows"
        )
        out = out.rename(columns={val_disp: value_header, "0": value_header})

    # ---- honest plain-language note ----
    what = (f"{_PRETTY[func]} of '{val}'" if val else "row counts")
    by = " × ".join([", ".join(row_disp)] + ([col_disp] if col_disp else []))
    note = f"Built a pivot summary: {what} by {by} — {len(out)} rows × {len(out.columns)} columns"
    if want_margins or (percent and show_totals):
        note += " with totals"
        if want_margins and func == "mean":
            note += f" (the '{margins_name}' line is the true overall average of the raw data, not an average of averages)"
    note += "."
    if percent:
        scope = {"grand": "the grand total", "row": "its row total", "column": "its column total"}[percent]
        note += f" Values are each cell's % of {scope}" + (f" of '{val}'." if val else " of the row count.")
    ignored = []
    if blanks:
        ignored.append(f"{blanks} blank cell{'s' if blanks != 1 else ''}")
    if bad_num:
        ignored.append(f"{bad_num} non-numeric cell{'s' if bad_num != 1 else ''}")
    if ignored and val:
        note += f" (ignored {' and '.join(ignored)} in '{val}')"
    for bit in note_bits:
        note += f" · {bit}"

    directive = None
    if live:
        directive = {
            "type": "pivot_formula",
            "source_df": src[[*row_disp, *([col_disp] if col_disp else []), val_disp]].copy(),
            "rows": row_disp,
            "column": col_disp,
            "value": val_disp,
            "func": _EXCEL_FUNC[func],
            "totals": bool(show_totals),
            "grid_rows": len(out),
            "grid_cols": len(out.columns),
        }
        fn = "PIVOTBY" if colf else "GROUPBY"
        note += (
            f" The saved file uses a live ={fn}() formula (source data on its own "
            f"sheet), so it recalculates as you edit — note: {fn} needs Microsoft 365; "
            f"older Excel shows #NAME?. The preview shows the computed values."
        )
    return out, note, directive


def _total_key(pt: pd.DataFrame, margins_name: str):
    """Index key for a hand-added totals row ('Total' or ('Total','',…) for
    multi-level row fields)."""
    if isinstance(pt.index, pd.MultiIndex):
        return (margins_name,) + ("",) * (pt.index.nlevels - 1)
    return margins_name
