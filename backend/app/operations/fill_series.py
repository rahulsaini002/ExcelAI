"""Engine Phase 1.10 — fill series + named ranges (Areas 3, 1).

fill_series  {"series_type": "numbers"|"months"|"weekdays"|"dates", "name",
              "start", "step", "count", "start_date", "every"}
  Fits the working table -> a new COLUMN ("number the rows 1-100"); a standalone
  length -> its own new SHEET ("list the 12 months"), so nothing is overwritten.

name_range   {"range_name": "Prices", "column": "Price"}
  Registers a workbook DefinedName over the column's data range AND (within the same
  plan) lets later formulas say {Prices}/{Prices:} — execute_multi substitutes the
  alias textually before the formula runs.
"""
from __future__ import annotations

import calendar
import re

import pandas as pd

from .base import OperationError

SERIES_TYPES = {"numbers", "months", "weekdays", "dates"}
_WEEKDAYS = {d.lower(): i for i, d in enumerate(calendar.day_name)}  # monday=0


def build_series(op: dict, table_rows: int) -> tuple[list, str, bool]:
    """Returns (values, description, fits_table)."""
    stype = (op.get("series_type") or "").strip().lower()
    if stype not in SERIES_TYPES:
        raise OperationError(
            f"I don't know the series type '{stype or '(none)'}' — I can fill numbers, "
            "months, weekdays, or dates."
        )
    count = op.get("count")

    if stype == "numbers":
        start = float(op.get("start", 1) if op.get("start") is not None else 1)
        step = float(op.get("step", 1) if op.get("step") is not None else 1)
        if step == 0:
            raise OperationError("A number series needs a non-zero step.")
        end = op.get("end")
        if count is None and end is not None:
            count = int((float(end) - start) / step) + 1
        n = int(count) if count is not None else table_rows
        if not 1 <= n <= 1_000_000:
            raise OperationError("A series can have between 1 and 1,000,000 values.")
        vals = [start + i * step for i in range(n)]
        vals = [int(v) if float(v).is_integer() else v for v in vals]
        desc = f"numbers from {vals[0]:g} step {step:g}"
    elif stype == "months":
        n = int(count) if count is not None else 12
        if not 1 <= n <= 1200:
            raise OperationError("A month series can have between 1 and 1,200 entries.")
        start_m = int(op.get("start", 1) if op.get("start") is not None else 1)
        vals = [calendar.month_name[((start_m - 1 + i) % 12) + 1] for i in range(n)]
        desc = f"the {n} month name{'s' if n != 1 else ''}"
    elif stype == "weekdays":
        n = int(count) if count is not None else 7
        vals = [calendar.day_name[i % 7] for i in range(n)]
        desc = f"the {n} weekday name{'s' if n != 1 else ''}"
    else:  # dates
        raw = op.get("start_date")
        try:
            start_d = pd.to_datetime(str(raw)) if raw else pd.Timestamp.today().normalize()
        except Exception:
            raise OperationError(f"I couldn't read '{raw}' as the series start date.")
        every = (op.get("every") or "day").strip().lower()
        n = int(count) if count is not None else (table_rows if table_rows else 10)
        if not 1 <= n <= 100_000:
            raise OperationError("A date series can have between 1 and 100,000 entries.")
        if every in _WEEKDAYS:  # "every monday": first shift onto that weekday
            shift = (_WEEKDAYS[every] - start_d.dayofweek) % 7
            first = start_d + pd.Timedelta(days=shift)
            vals = [first + pd.Timedelta(weeks=i) for i in range(n)]
            desc = f"{n} dates, every {every.capitalize()}"
        elif every in ("day", "daily"):
            vals = [start_d + pd.Timedelta(days=i) for i in range(n)]
            desc = f"{n} daily dates"
        elif every in ("week", "weekly"):
            vals = [start_d + pd.Timedelta(weeks=i) for i in range(n)]
            desc = f"{n} weekly dates"
        elif every in ("month", "monthly"):
            vals = [start_d + pd.DateOffset(months=i) for i in range(n)]
            desc = f"{n} monthly dates"
        else:
            raise OperationError(
                f"I don't understand '{every}' — say daily, weekly, monthly, or a "
                "weekday like Monday."
            )
        vals = [v.normalize() for v in vals]
    return vals, desc, len(vals) == table_rows


def validate_range_name(name: str, existing_columns) -> str:
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise OperationError(
            "A range name must start with a letter and use only letters, numbers, and "
            "underscores (no spaces) — e.g. 'Prices'."
        )
    if name.lower() in {str(c).strip().lower() for c in existing_columns}:
        raise OperationError(
            f"'{name}' is already a column name — pick a different range name to avoid "
            "confusion in formulas."
        )
    return name
