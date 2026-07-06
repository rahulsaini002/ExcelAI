"""Shared building blocks for the trusted operations.

These have NO dependency on the executor, so every operation module can import them
freely without creating a circular import (executor -> operations -> base).
"""
from __future__ import annotations

import warnings

import pandas as pd


class OperationError(ValueError):
    """Raised when an operation can't be carried out (e.g. unknown column)."""


def require_columns(df: pd.DataFrame, columns: list[str]) -> None:
    """Raise a friendly OperationError if any of `columns` is missing from `df`."""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        names = ", ".join(f"'{c}'" for c in missing)
        avail = ", ".join(map(str, df.columns))
        raise OperationError(
            f"I don't see the column{'s' if len(missing) != 1 else ''} {names}. "
            f"Available columns: {avail}."
        )


def to_datetime(obj):
    """pd.to_datetime(errors='coerce') without the noisy 'could not infer format'
    warning — we deliberately accept mixed/unparseable values as NaT.

    A single column can mix date formats (e.g. "01/01/2025", "Jan 03 2025",
    "04-Jan-2025"). pandas infers ONE format and NaTs the rest, which broke date
    sort/filter on such columns. So when a Series parses only partially, we retry
    with format="mixed" (each value parsed on its own) and keep whichever parses
    more. The retry only fires when the default parsed SOME but not all values, so
    plain text and clean single-format date columns pay no extra cost.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = pd.to_datetime(obj, errors="coerce")
        if isinstance(obj, pd.Series):
            failed = result.isna() & obj.notna()
            if result.notna().any() and bool(failed.any()):
                retry = pd.to_datetime(obj, errors="coerce", format="mixed")
                if int(retry.notna().sum()) > int(result.notna().sum()):
                    result = retry
        return result
