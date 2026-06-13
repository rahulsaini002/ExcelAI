"""Executes a validated operation plan against a DataFrame.

This is the trusted layer. The LLM only proposes a plan; this code decides
whether each operation is valid and carries it out with pandas. Every operation
returns a plain-language note describing what *actually* happened (with real
counts), so the user always sees an honest account — never a silent wrong result.
"""
from __future__ import annotations

import re

import pandas as pd

# Only column-name tokens, arithmetic operators, parens, numbers, and spaces are
# allowed in a formula once column placeholders are substituted. This guards the
# df.eval() call against anything that isn't simple arithmetic.
_SAFE_FORMULA = re.compile(r"^[\s\d.+\-*/()`\w]*$")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class OperationError(ValueError):
    """Raised when an operation can't be carried out (e.g. unknown column)."""


def execute_plan(df: pd.DataFrame, operations: list[dict]) -> tuple[pd.DataFrame, list[str]]:
    """Apply each operation in order. Returns the result and a list of notes."""
    notes: list[str] = []
    for op in operations:
        action = op.get("action")
        if action == "sort":
            df, note = _sort(df, op)
        elif action == "remove_duplicates":
            df, note = _remove_duplicates(df, op)
        elif action == "add_formula_column":
            df, note = _add_formula_column(df, op)
        else:
            raise OperationError(f"Unknown operation: {action!r}")
        notes.append(note)
    return df, notes


def _require_columns(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise OperationError(
            f"These columns aren't in the sheet: {', '.join(missing)}. "
            f"Available columns: {', '.join(map(str, df.columns))}."
        )


def _sort(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    columns = op.get("columns") or []
    if not columns:
        raise OperationError("Sort needs at least one column.")
    _require_columns(df, columns)

    orders = op.get("orders") or []
    # Default any unspecified order to ascending.
    ascending = [(orders[i] if i < len(orders) else "asc") != "desc" for i in range(len(columns))]

    df = df.sort_values(by=columns, ascending=ascending, kind="stable").reset_index(drop=True)

    parts = [f"{c} {'descending' if not asc else 'ascending'}" for c, asc in zip(columns, ascending)]
    return df, f"Sorted by {', '.join(parts)}."


def _remove_duplicates(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    columns = op.get("columns") or None
    if columns:
        _require_columns(df, columns)

    before = len(df)
    df = df.drop_duplicates(subset=columns, keep="first").reset_index(drop=True)
    removed = before - len(df)

    basis = f" based on {', '.join(columns)}" if columns else ""
    return df, f"Removed {removed} duplicate row{'s' if removed != 1 else ''}{basis}."


def _add_formula_column(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    name = (op.get("name") or "").strip()
    formula = op.get("formula") or ""
    if not name:
        raise OperationError("A new formula column needs a name.")
    if not formula:
        raise OperationError(f"No formula provided for column '{name}'.")

    referenced = _PLACEHOLDER.findall(formula)
    _require_columns(df, referenced)

    # Turn "{Qty} * {Price}" into "`Qty` * `Price`" so df.eval can resolve names
    # that contain spaces or punctuation via backtick quoting.
    expr = _PLACEHOLDER.sub(lambda m: f"`{m.group(1)}`", formula)
    if not _SAFE_FORMULA.match(expr):
        raise OperationError(
            f"Formula '{formula}' contains unsupported characters. "
            "Use only column names and + - * / ( )."
        )

    try:
        df = df.copy()
        df[name] = df.eval(expr, engine="python")
    except Exception as exc:  # pandas raises a variety of errors here
        raise OperationError(f"Couldn't compute '{name}' from '{formula}': {exc}") from exc

    return df, f"Added column '{name}' = {formula}."
