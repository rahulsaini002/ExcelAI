"""Executes a validated operation plan against a DataFrame.

This is the trusted layer ("the Hands"). The LLM only proposes a plan; this code
decides whether each operation is valid and carries it out with pandas. Every
operation returns a plain-language note describing what *actually* happened (with
real counts), so the user always sees an honest account — never a silent wrong
result.

Each operation is a small function `_name(df, op) -> (df, note)`. `execute_plan`
just dispatches on the `action` field and chains the operations in order.
"""
from __future__ import annotations

import ast
import difflib
import re

import numpy as np
import pandas as pd

# Shared helpers + the per-operation modules live in the operations/ package. We
# import the shared helpers under their original private names so the operations
# that still live in this file keep working unchanged.
from .operations.base import (
    OperationError,
    require_columns as _require_columns,
    to_datetime as _to_datetime,
)
from .operations.sort import sort as _sort
from .operations.chart import chart as _chart
from .operations.dashboard import dashboard as _dashboard
from .operations.reshape import unpivot as _unpivot, pivot as _pivot, transpose as _transpose
from .operations.conditional_format import conditional_format as _conditional_format
from .operations.layout import layout_format as _layout_format
from .operations.excel_table import excel_table as _excel_table
from .operations.pivot_summary import pivot_summary as _pivot_summary
from .operations.statistics import statistics as _statistics
from .operations.explain_notes import explain_changes as _explain_changes
from .operations.fill_series import (
    build_series as _build_series,
    validate_range_name as _validate_range_name,
)
from .operations.goal_seek import goal_seek as _goal_seek
from .operations.sheets_mgmt import sheet_op as _sheet_op
from .operations.validation import data_validation as _data_validation
from .operations.split_merge import (
    fill_by_example as _fill_by_example,
    merge_columns as _merge_columns,
    split_column as _split_column,
)
from .operations.formula_functions import (
    M365_FUNCS,
    REDIRECTS,
    SUPPORTED as _REGISTRY_FUNCS,
    _Range,
    _Spill,
    _apply as _apply_registry_func,
)

# Only column-name tokens, arithmetic operators, parens, numbers, and spaces are
# allowed in a formula once column placeholders are substituted. This guards the
# df.eval() call against anything that isn't simple arithmetic.
_SAFE_FORMULA = re.compile(r"^[\s\d.+\-*/()`\w]*$")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
# For matching column names that mean the same thing despite case/spacing/punctuation.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Characters not allowed in Excel sheet/tab names.
_NON_SHEET = re.compile(r"[:\\/?*\[\]]")


class OperationCancelled(Exception):
    """The caller asked to stop between steps (Track 4 item 6 — a job deadline).

    Distinct from an operation FAILING: nothing about the data was wrong, we simply ran
    out of the time budget. It carries how far we had got so the caller can say "stopped
    after step 2 of 5" instead of just "timed out".

    This is the ONE exception `on_step` is allowed to raise. The progress-callback guard
    swallows everything else — a broken reporter must never fail a real execution — but a
    deliberate cancellation has to be able to get out, so it is re-raised.
    """

    def __init__(self, completed_steps: int, total_steps: int, reason: str = ""):
        super().__init__(reason or "The run was stopped before it finished.")
        self.completed_steps = completed_steps  # steps fully done before stopping
        self.total_steps = total_steps
        self.reason = reason


class MultiStepError(Exception):
    """A later step of a multi-step plan failed, but earlier steps succeeded.

    Carries the partial result (the file as of the last good step) so the caller
    can still hand the user a downloadable file plus a clear "step N failed" message,
    instead of throwing the completed work away (PRD multi-step MS-b).
    """

    def __init__(self, partial_result, partial_name, notes, format_ops, failed_step, reason):
        super().__init__(reason)
        self.partial_result = partial_result  # df or workbook-dict as of the last good step
        self.partial_name = partial_name
        self.notes = notes                    # plain-language notes for completed steps
        self.format_ops = format_ops          # render directives from completed steps
        self.failed_step = failed_step        # 1-based index of the step that failed
        self.reason = reason                  # the friendly OperationError message


def _apply_one(
    df: pd.DataFrame, op: dict, tables: dict[str, pd.DataFrame]
) -> tuple[pd.DataFrame, str | None, dict | None]:
    """Run a single (non-merge) operation on one table.

    Returns (new_df, note, format_directive). `format_directive` is non-None only
    for format_cells (formatting is applied later, when the file is saved).
    `tables` is the full namespace so lookups can reach other tables.
    """
    action = op.get("action")
    if action == "sort":
        df, note = _sort(df, op)
    elif action == "filter":
        df, note = _filter(df, op)
    elif action == "limit":
        df, note = _limit(df, op)
    elif action == "remove_duplicates":
        df, note = _remove_duplicates(df, op)
    elif action == "fill_missing":
        df, note = _fill_missing(df, op)
    elif action == "drop_missing":
        df, note = _drop_missing(df, op)
    elif action == "drop_invalid":
        df, note = _drop_invalid(df, op)
    elif action == "trim":
        df, note = _trim(df, op)
    elif action == "add_formula_column":
        df, note, directive = _add_formula_column(df, op, tables)
        return df, note, directive
    elif action == "lookup":
        df, note, directive = _lookup(df, op, tables)
        return df, note, directive
    elif action == "aggregate":
        df, note = _aggregate(df, op)
    elif action == "find_replace":
        df, note = _find_replace(df, op)
    elif action == "rename_columns":
        df, note = _rename_columns(df, op)
    elif action == "drop_columns":
        df, note = _drop_columns(df, op)
    elif action == "select_columns":
        df, note = _select_columns(df, op)
    elif action == "flag_missing":
        note, directive = _flag_missing(df, op)
        return df, note, directive
    elif action == "format_cells":
        note, directive = _format_cells(df, op)
        return df, note, directive
    elif action == "conditional_format":
        df, note, directive = _conditional_format(df, op, lambda f, d: _eval_formula(f, d, tables))
        return df, note, directive
    elif action == "layout_format":
        df, note, directive = _layout_format(df, op)
        return df, note, directive
    elif action == "data_validation":
        df, note, directive = _data_validation(df, op)
        return df, note, directive
    elif action == "excel_table":
        df, note, directive = _excel_table(df, op)
        return df, note, directive
    elif action == "goal_seek":
        df, note, directive = _goal_seek(df, op, tables, _eval_formula)
        return df, note, directive
    elif action == "split_column":
        df, note = _split_column(df, op)
    elif action == "merge_columns":
        df, note, directive = _merge_columns(df, op)
        return df, note, directive
    elif action == "fill_by_example":
        df, note = _fill_by_example(df, op)
    elif action == "chart":
        note, directive = _chart(df, op)
        return df, note, directive
    elif action == "dashboard":
        note, directive = _dashboard(df, op)
        return df, note, directive
    elif action == "unpivot":
        df, note = _unpivot(df, op)
    elif action == "pivot":
        df, note = _pivot(df, op)
    elif action == "pivot_summary":
        df, note, directive = _pivot_summary(df, op)
        return df, note, directive
    elif action == "statistics":
        df, note, directive = _statistics(df, op)
        return df, note, directive
    elif action == "transpose":
        df, note = _transpose(df, op)
    elif action == "set_cells":
        df, note = _set_cells(df, op)
    elif action == "forecast":
        df, note, directive = _forecast(df, op)
        return df, note, directive
    elif action == "what_if":
        df, note, directive = _what_if(df, op)
        return df, note, directive
    elif action == "detect_anomalies":
        df, note, directive = _detect_anomalies(df, op)
        return df, note, directive
    else:
        # Reached when a plan names an action this engine doesn't have — a hand-edited
        # plan, an old saved workflow, or a model that invented one. The old text was
        # "Unknown operation: 'x'": accurate, but it read like a stack trace and left the
        # user with nowhere to go.
        raise OperationError(
            f"I don't have an operation called '{action}'. If you edited the plan, check "
            "that step's \"action\"; otherwise just describe what you want in your own "
            "words and I'll work out the steps."
        )
    return df, note, None


def execute_plan(
    df: pd.DataFrame,
    operations: list[dict],
    sheets: dict[str, pd.DataFrame] | None = None,
) -> tuple[pd.DataFrame, list[str], list[dict]]:
    """Apply operations to a single working table (single-file path).

    Returns the resulting DataFrame, plain-language notes, and formatting
    directives. `sheets` is the table namespace lookups can reach.
    """
    sheets = sheets or {}
    notes: list[str] = []
    format_ops: list[dict] = []
    for op in operations:
        df, note, directive = _apply_one(df, op, sheets)
        if note is not None:
            notes.append(note)
        if directive is not None:
            format_ops.append(directive)
    return df, notes, format_ops


def execute_multi(
    tables: dict[str, pd.DataFrame],
    primary: str,
    operations: list[dict],
    on_step=None,
) -> tuple[pd.DataFrame, str, list[str], list[dict]]:
    """Apply operations across multiple named tables.

    A "working table" starts as `primary`. Each operation acts on the table named
    by its "table" field, or the working table if none is given; the result
    becomes the new working table. `merge` combines several tables into a new one.

    `on_step(index0, action)` is an optional progress callback (Track 4 item 1), invoked
    just BEFORE each step runs — so it fires only when a step is genuinely reached, never
    on a timer. It is best-effort: any exception it raises is swallowed, because a broken
    progress reporter must never be able to fail a real execution. Default None keeps the
    signature backward-compatible for every existing caller.

    Returns (result_df, result_table_name, notes, format_ops).
    """
    tables = dict(tables)  # don't mutate the caller's dict
    originals = dict(tables)  # pre-plan snapshot (ops copy, never mutate in place)
    working = primary
    notes: list[str] = []
    format_ops: list[dict] = []

    workbook: dict[str, pd.DataFrame] | None = None  # set by combine_sheets
    workbook_name = "combined"
    namespace_changed = False  # sheet ops make the WHOLE workbook the result
    aliases: dict[str, str] = {}  # name_range: plan-scoped {RangeName -> column}

    for step_idx, op in enumerate(operations):
        if on_step is not None:
            # Best-effort: a progress reporter that throws must not fail a real run —
            # EXCEPT OperationCancelled, which is the reporter deliberately stopping us
            # (a job deadline). A reporter may cancel; it may not fail.
            try:
                on_step(step_idx, (op or {}).get("action"))
            except OperationCancelled:
                raise
            except Exception:
                pass
        try:
            # Named-range aliases: later formulas in the SAME plan may say {Prices:} —
            # substitute textually before the op runs.
            if aliases and op.get("formula"):
                fixed = op["formula"]
                for rn, col in aliases.items():
                    fixed = re.sub(r"\{\s*" + re.escape(rn) + r"\s*(:?)\}",
                                   lambda m, c=col: "{" + c + m.group(1) + "}", fixed)
                if fixed != op["formula"]:
                    op = {**op, "formula": fixed}

            if op.get("action") == "fill_series":
                base = tables[working]
                vals, desc, fits = _build_series(op, len(base))
                col_name = (op.get("name") or "Series").strip() or "Series"
                if fits:
                    if col_name in base.columns and not op.get("overwrite"):
                        raise OperationError(
                            f"A column called '{col_name}' already exists. Use a "
                            "different name, or confirm you want to overwrite it."
                        )
                    df = base.copy()
                    df[col_name] = vals
                    tables[working] = df
                    notes.append(f"Filled a new column '{col_name}' with {desc}.")
                else:
                    if col_name in tables:
                        raise OperationError(f"A sheet called '{col_name}' already exists.")
                    tables[col_name] = pd.DataFrame({col_name: vals})
                    namespace_changed = True
                    notes.append(
                        f"Put {desc} on a new sheet '{col_name}' ({len(vals):,} rows — "
                        f"the working table has {len(base):,}, so a column wouldn't fit)."
                    )
                continue

            if op.get("action") == "name_range":
                base = tables[working]
                rn = _validate_range_name(op.get("range_name"), base.columns)
                col = (op.get("column") or "").strip()
                if col not in base.columns:
                    raise OperationError(f"I couldn't find the column '{col}' to name.")
                aliases[rn] = col
                format_ops.append({"type": "defined_name", "name": rn, "column": col})
                notes.append(
                    f"Named the '{col}' data range '{rn}'. Formulas in this request can "
                    f"use {{{rn}:}}, and the saved file carries the defined name."
                )
                continue

            if op.get("action") == "sheet_op":
                tables, working, note, directive, changed = _sheet_op(tables, working, op)
                notes.append(note)
                if directive is not None:
                    format_ops.append(directive)
                namespace_changed = namespace_changed or changed
                continue

            if op.get("action") == "explain_changes":
                # Diff the plan's ORIGINAL table against the current one (Phase 1.9).
                base = originals.get(working, originals.get(primary))
                note, directive = _explain_changes(base, tables[working], notes)
                notes.append(note)
                if directive is not None:
                    format_ops.append(directive)
                continue

            if op.get("action") == "merge":
                df, new_name, note = _merge(tables, op)
                tables[new_name] = df
                working = new_name
                notes.append(note)
                continue

            if op.get("action") == "combine_sheets":
                workbook, workbook_name, note = _combine_sheets(tables, op)
                notes.append(note)
                continue

            target = op.get("table") or working
            if target not in tables:
                raise OperationError(
                    f"I don't have a table named '{target}'. "
                    f"Available tables: {', '.join(tables)}."
                )
            df, note, directive = _apply_one(tables[target], op, tables)
            tables[target] = df
            working = target
            if note is not None:
                notes.append(note)
            if directive is not None:
                format_ops.append(directive)
        except OperationError as exc:
            # If the VERY FIRST step fails there's no partial result to keep — let the
            # normal error path explain it. If a LATER step fails, stop here but hand
            # back the file reflecting the steps that already completed (MS-b).
            if step_idx == 0:
                raise
            partial = workbook if workbook is not None else tables[working]
            partial_name = workbook_name if workbook is not None else working
            raise MultiStepError(
                partial, partial_name, notes, format_ops, step_idx + 1, str(exc)
            ) from exc

    if workbook is not None:
        return workbook, workbook_name, notes, format_ops
    if namespace_changed:
        # Sheet management restructured the workbook — every tab is part of the result.
        return tables, working, notes, format_ops
    return tables[working], working, notes, format_ops


def _combine_sheets(
    tables: dict[str, pd.DataFrame], op: dict
) -> tuple[dict[str, pd.DataFrame], str, str]:
    """Combine several tables into ONE workbook, each table on its own sheet/tab.

    Different from `merge` (which stacks rows into a single table). Returns a dict
    of {sheet_name: df} for the serializer to write as separate tabs.
    """
    names = op.get("sheet_tables") or op.get("merge_tables") or list(tables.keys())
    missing = [n for n in names if n not in tables]
    if missing:
        raise OperationError(
            f"Can't combine — I don't have table(s): {', '.join(missing)}. "
            f"Available: {', '.join(tables)}."
        )
    if len(names) < 2:
        raise OperationError(
            "Putting each table on its own sheet needs at least two tables, and only one "
            "was loaded. Upload the other file (or files) and ask again."
        )

    sheets: dict[str, pd.DataFrame] = {}
    for name in names:
        # Sheet names must be <=31 chars and avoid : \ / ? * [ ]; keep them unique.
        base = _NON_SHEET.sub(" ", str(name)).strip()[:28] or "Sheet"
        label = base
        i = 2
        while label in sheets:
            label = f"{base} {i}"[:31]
            i += 1
        sheets[label] = tables[name]

    out_name = op.get("new_table") or "combined"
    note = (
        f"Combined {len(names)} tables into one workbook, each on its own sheet: "
        f"{', '.join(names)}."
    )
    return sheets, out_name, note


def _norm_col(name: str) -> str:
    """Normalize a column name for matching: lowercase, drop spaces/underscores/punct.
    So 'Customer ID', 'customer_id', and 'CustomerID' all become 'customerid'."""
    return _NON_ALNUM.sub("", str(name).lower())


def _merge_type_conflicts(frames: list[pd.DataFrame]) -> list[str]:
    """Columns (after unification) that hold numbers in some files but text in others.

    When such a column is stacked, pandas silently turns it into text — so a column
    that's numeric in most files could quietly become unsortable. We surface these so
    the user can fix the offending file instead of being surprised later.
    """
    kinds: dict[str, set[str]] = {}
    for f in frames:
        for col in f.columns:
            series = f[col]
            nonblank = series[~_blank_mask(series)]
            if len(nonblank) == 0:
                continue  # all-blank in this file tells us nothing about its type
            if pd.api.types.is_numeric_dtype(series):
                kind = "num"
            else:
                kind = "num" if pd.to_numeric(nonblank, errors="coerce").notna().all() else "text"
            kinds.setdefault(str(col), set()).add(kind)
    return [c for c, ks in kinds.items() if {"num", "text"} <= ks]


def _merge(tables: dict[str, pd.DataFrame], op: dict) -> tuple[pd.DataFrame, str, str]:
    """Stack several tables into one, lining up columns that mean the same thing.

    Two kinds of column unification happen:
      1. Synonym groups from the plan ("column_groups") — the Brain decides that
         e.g. client_id and cust_no both mean Customer_ID.
      2. Automatic — columns that differ only in case/spacing/punctuation are
         unified to the first spelling seen.
    Genuinely different columns are kept separate (union of columns).
    """
    names = op.get("merge_tables") or list(tables.keys())
    missing = [n for n in names if n not in tables]
    if missing:
        raise OperationError(
            f"Can't merge — I don't have table(s): {', '.join(missing)}. "
            f"Available: {', '.join(tables)}."
        )
    if len(names) < 2:
        raise OperationError(
            "Merging needs at least two tables, and only one was loaded. Upload the "
            "second file and ask again — I'll match up the columns for you."
        )

    # 1. Synonym map from the plan: each alias -> the unified (canonical) name.
    alias_to_canon: dict[str, str] = {}
    for group in op.get("column_groups") or []:
        canon = (group.get("name") or "").strip()
        if not canon:
            continue
        for alias in group.get("aliases") or []:
            alias_to_canon[str(alias)] = canon

    # 2. Auto map: first spelling seen for each normalized name becomes canonical.
    #    A later column whose normalized name is a CLOSE MATCH (likely a typo, e.g.
    #    'Custmer_ID' vs 'Customer_ID') is unified to the earlier one — Phase 3.1 fuzzy.
    norm_to_canon: dict[str, str] = {}
    fuzzy_norms: set[str] = set()
    for name in names:
        for col in tables[name].columns:
            key = _norm_col(col)
            if not key or key in norm_to_canon:
                continue
            close = (difflib.get_close_matches(key, list(norm_to_canon), n=1, cutoff=0.85)
                     if len(key) >= 4 else [])
            if close:
                norm_to_canon[key] = norm_to_canon[close[0]]  # unify to the existing spelling
                fuzzy_norms.add(key)
            else:
                norm_to_canon[key] = str(col)

    unified: set[tuple[str, str]] = set()
    fuzzy_unified: set[tuple[str, str]] = set()
    frames = []
    for name in names:
        df = tables[name]
        rename = {}
        for col in df.columns:
            canon = alias_to_canon.get(col) or norm_to_canon.get(_norm_col(col))
            if canon and canon != col:
                rename[col] = canon
                (fuzzy_unified if _norm_col(col) in fuzzy_norms else unified).add((str(col), canon))
        if rename:
            df = df.rename(columns=rename)
        frames.append(df)

    new_name = op.get("new_table") or "merged"

    # If the tables share NO column names, they're unrelated data — stacking them
    # vertically would leave a staircase of blanks. Place them SIDE BY SIDE instead
    # (aligned by row). If they share columns, stack rows (combine the lists).
    col_sets = [set(f.columns) for f in frames]
    total_cols = sum(len(s) for s in col_sets)
    union_cols = len(set().union(*col_sets)) if col_sets else 0
    disjoint = union_cols == total_cols

    if disjoint:
        merged = pd.concat([f.reset_index(drop=True) for f in frames], axis=1)
        note = (
            f"Placed {len(names)} tables ({', '.join(names)}) side by side in one sheet "
            f"'{new_name}' (no shared columns to stack on) — {len(merged)} rows, "
            f"{len(merged.columns)} columns."
        )
        return merged, new_name, note

    merged = pd.concat(frames, ignore_index=True, sort=False)
    note = (
        f"Merged {len(names)} tables ({', '.join(names)}) into '{new_name}' — "
        f"{len(merged)} rows, {len(merged.columns)} columns."
    )
    if unified:
        pairs = ", ".join(f"'{a}'→'{b}'" for a, b in sorted(unified))
        note += f" Unified columns with the same meaning: {pairs}."
    if fuzzy_unified:
        pairs = ", ".join(f"'{a}'→'{b}'" for a, b in sorted(fuzzy_unified))
        note += f" Treated near-identical column names as the same (likely typos): {pairs}."
    conflicts = _merge_type_conflicts(frames)
    if conflicts:
        cols = ", ".join(f"'{c}'" for c in conflicts)
        many = len(conflicts) != 1
        note += (
            f" Heads up: {'columns' if many else 'column'} {cols} "
            f"{'have' if many else 'has'} numbers in some files and text in others — "
            f"kept as text so nothing is lost."
        )
    return merged, new_name, note  # caller stores under new_name


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _blank_mask(series: pd.Series) -> pd.Series:
    """True where a cell is empty (NaN, or a string that's only whitespace)."""
    return series.isna() | (series.astype(str).str.strip() == "")


def _norm_key(series: pd.Series) -> pd.Series:
    """Normalize keys for matching: trim, lowercase, and treat 123 == '123'."""

    def f(v):
        if pd.isna(v):
            return None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v).strip().lower()

    return series.map(f)


# --------------------------------------------------------------------------- #
# Operations  (one module per operation lives in operations/; sort.py is done,
# the rest still live here and can be extracted the same way)
# --------------------------------------------------------------------------- #
def _limit(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Keep only the first (or last) N rows — e.g. "top 100" after a sort."""
    n = op.get("count")
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise OperationError("Tell me how many rows to keep (a positive whole number).")
    if n <= 0:
        raise OperationError("The number of rows to keep must be greater than zero.")
    from_end = bool(op.get("from_end"))
    kept = (df.tail(n) if from_end else df.head(n)).reset_index(drop=True)
    where = "last" if from_end else "top"
    return kept, f"Kept the {where} {len(kept)} row{'s' if len(kept) != 1 else ''} (of {len(df)})."


# Operators understood by the filter operation.
_NUMERIC_OPS = {"greater_than", "less_than", "greater_or_equal", "less_or_equal", "between"}
_TEXT_OPS = {"contains", "starts_with", "ends_with"}

# Plain-language names for every operator _condition_mask handles, used to tell a user
# what they CAN filter with when they ask for something we don't have. Built from the
# sets above plus the ones handled inline, and asserted complete by a test — a
# hand-written list in an error message drifts, and an error that recommends an operator
# the engine doesn't support is worse than one that stays vague.
_OPERATOR_WORDS = {
    "equals": "is",
    "not_equals": "is not",
    "in": "is one of",
    "not_in": "is not one of",
    "contains": "contains",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "greater_than": "is greater than",
    "less_than": "is less than",
    "greater_or_equal": "is at least",
    "less_or_equal": "is at most",
    "between": "is between",
    "is_blank": "is blank",
    "not_blank": "is not blank",
}


def supported_filter_operators() -> list[str]:
    """The human names of every filter operator, for error copy and for tests."""
    return [_OPERATOR_WORDS[k] for k in sorted(_OPERATOR_WORDS)]


def _filter(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    conditions = op.get("conditions") or []
    if not conditions:
        raise OperationError("Filter needs at least one condition.")
    combine = (op.get("combine") or "and").lower()
    if combine not in {"and", "or"}:  # free-text field now — don't silently AND
        raise OperationError(
            f"I don't understand combining conditions with '{combine}' — use and / or."
        )

    masks: list[pd.Series] = []
    descriptions: list[str] = []
    for cond in conditions:
        column = cond.get("column")
        operator = (cond.get("operator") or "").lower()
        if not column:
            raise OperationError(
                "One of the filter conditions doesn't say which column to look at. Tell "
                "me the column to filter on — e.g. \"keep rows where Region is North\"."
            )
        _require_columns(df, [column])
        mask, desc = _condition_mask(df[column], column, operator, cond.get("value"), cond.get("value2"), cond.get("values"))
        masks.append(mask)
        descriptions.append(desc)

    if combine == "or":
        final = masks[0]
        for m in masks[1:]:
            final = final | m
        joiner = " or "
    else:
        final = masks[0]
        for m in masks[1:]:
            final = final & m
        joiner = " and "

    before = len(df)
    df = df[final.fillna(False)].reset_index(drop=True)
    return df, f"Kept {len(df)} of {before} rows where {joiner.join(descriptions)}."


def _condition_mask(series, column, operator, value, value2, values=None):
    """Build a boolean mask for one filter condition, plus a plain description."""
    if operator == "is_blank":
        return _blank_mask(series), f"{column} is blank"
    if operator == "not_blank":
        return ~_blank_mask(series), f"{column} is not blank"

    if operator in {"equals", "not_equals"}:
        target = _norm_key(pd.Series([value])).iloc[0]
        eq = _norm_key(series) == target
        if operator == "equals":
            return eq, f"{column} = {value!r}"
        return ~eq, f"{column} ≠ {value!r}"

    if operator in {"in", "not_in"}:
        opts = values if values else ([value] if value is not None else [])
        if not opts:
            raise OperationError(f"The '{operator}' filter on {column} needs a list of values.")
        targets = set(_norm_key(pd.Series(opts)))
        isin = _norm_key(series).isin(targets)
        shown = ", ".join(map(str, opts))
        if operator == "in":
            return isin, f"{column} is one of [{shown}]"
        return ~isin, f"{column} is not one of [{shown}]"

    if operator in _TEXT_OPS:
        if value is None:
            raise OperationError(f"The '{operator}' filter on {column} needs a value.")
        # Nullable string keeps blanks as <NA> (so None doesn't become "None" and
        # cause false matches); blanks never match a text condition.
        text = series.astype("string")
        low = text.str.lower()
        v = str(value).lower()
        if operator == "contains":
            mask = text.str.contains(re.escape(str(value)), case=False, na=False)
            return mask, f"{column} contains {value!r}"
        if operator == "starts_with":
            return low.str.startswith(v).fillna(False), f"{column} starts with {value!r}"
        return low.str.endswith(v).fillna(False), f"{column} ends with {value!r}"

    if operator in _NUMERIC_OPS:
        raw = [value, value2] if operator == "between" else [value]
        comp, vals = _comparable(series, column, raw)
        v1 = vals[0]
        if operator == "greater_than":
            return comp > v1, f"{column} > {value}"
        if operator == "less_than":
            return comp < v1, f"{column} < {value}"
        if operator == "greater_or_equal":
            return comp >= v1, f"{column} ≥ {value}"
        if operator == "less_or_equal":
            return comp <= v1, f"{column} ≤ {value}"
        # between
        lo, hi = sorted([v1, vals[1]])
        return (comp >= lo) & (comp <= hi), f"{column} between {value} and {value2}"

    raise OperationError(
        f"I don't know how to filter with '{operator}'. I can check whether a column: "
        + ", ".join(supported_filter_operators())
        + ". Try rephrasing with one of those — e.g. \"keep rows where Amount is at "
        "least 500\"."
    )


def _comparable(series, column, raw_values):
    """Coerce a column and the comparison value(s) to a comparable type.

    Tries numbers (including numbers stored as text), then dates. Raises a
    friendly error if the column or the value can't be compared that way.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        vals = [_to_datetime(v) for v in raw_values]
        if any(pd.isna(v) for v in vals):
            raise OperationError(f"I couldn't read a date to compare with '{column}'.")
        return series, vals

    nums = pd.to_numeric(series, errors="coerce")
    if int(nums.notna().sum()) > 0:
        try:
            return nums, [float(v) for v in raw_values]
        except (TypeError, ValueError):
            pass  # value isn't numeric — maybe the column is really dates-as-text

    dates = _to_datetime(series)
    if int(dates.notna().sum()) > 0:
        vals = [_to_datetime(v) for v in raw_values]
        if any(pd.isna(v) for v in vals):
            raise OperationError(
                f"'{raw_values[0]}' can't be compared with '{column}'."
            )
        return dates, vals

    raise OperationError(
        f"Can't compare '{column}' with '{raw_values[0]}' — it isn't numbers or dates."
    )


def _norm_dup(series: pd.Series) -> pd.Series:
    """Normalize a column for duplicate detection: text is trimmed and lowercased
    (so 'a@x.com ' and 'A@X.com' count as the same); other types are left as-is."""
    if series.dtype == object or str(series.dtype) == "string":
        return series.astype("string").str.strip().str.lower()
    return series


def _remove_duplicates(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    columns = op.get("columns") or None
    if columns:
        _require_columns(df, columns)

    before = len(df)
    subset_cols = columns if columns else list(df.columns)
    # Compare on normalized keys (trimmed, case-insensitive for text) but keep the
    # original rows in the output.
    key = pd.DataFrame({c: _norm_dup(df[c]) for c in subset_cols}, index=df.index)
    df = df[~key.duplicated(keep="first")].reset_index(drop=True)
    removed = before - len(df)

    basis = f" based on {', '.join(columns)}" if columns else ""
    if removed == 0:
        return df, f"No duplicate rows found{basis}."
    return df, (
        f"Removed {removed} duplicate row{'s' if removed != 1 else ''}{basis} "
        "(kept the first of each)."
    )


def _fill_missing(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    columns = op.get("columns") or list(df.columns)
    _require_columns(df, columns)
    method = (op.get("fill_method") or "").lower().strip()
    fill_value = op.get("fill_value")
    where = ", ".join(columns) if op.get("columns") else "the sheet"
    df = df.copy()
    affected = 0

    # Simple fill: copy the previous ("previous"/ffill) or next ("next"/bfill) value.
    if method in {"previous", "ffill", "forward", "next", "bfill", "backward"}:
        pandas_method = "ffill" if method in {"previous", "ffill", "forward"} else "bfill"
        for col in columns:
            blanks = _blank_mask(df[col])
            s = df[col].mask(blanks)  # turn blanks (incl "") into NaN so fill works
            filled = s.ffill() if pandas_method == "ffill" else s.bfill()
            affected += int((blanks & filled.notna()).sum())
            df[col] = filled
        if affected == 0:
            return df, f"No missing values found in {where}."
        how = "the previous value" if pandas_method == "ffill" else "the next value"
        return df, f"Filled {affected} blank cell{'s' if affected != 1 else ''} in {where} using {how}."

    # Fixed value.
    if fill_value is None:
        raise OperationError(
            "Tell me what to fill blanks with — a fixed value like 'Unknown' or 0, "
            "or 'previous'/'next' to copy the neighbouring value."
        )
    for col in columns:
        blanks = _blank_mask(df[col])
        affected += int(blanks.sum())
        value = fill_value
        # If the column is numeric and the fill value looks numeric, keep it numeric.
        if pd.api.types.is_numeric_dtype(df[col]):
            try:
                value = float(fill_value)
                if value.is_integer():
                    value = int(value)
            except (TypeError, ValueError):
                pass
        df.loc[blanks, col] = value

    if affected == 0:
        return df, f"No missing values found in {where}."
    return df, f"Filled {affected} blank cell{'s' if affected != 1 else ''} in {where} with '{fill_value}'."


def _drop_missing(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    columns = op.get("columns") or list(df.columns)
    _require_columns(df, columns)

    before = len(df)
    blank_rows = pd.Series(False, index=df.index)
    for col in columns:
        blank_rows = blank_rows | _blank_mask(df[col])
    df = df[~blank_rows].reset_index(drop=True)
    removed = before - len(df)

    where = ", ".join(columns) if op.get("columns") else "any column"
    if removed == 0:
        return df, f"No missing values found in {where}."
    return df, f"Removed {removed} row{'s' if removed != 1 else ''} with blanks in {where}."


def _drop_invalid(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Remove rows whose value in the chosen column(s) isn't a valid number (or date).

    A cell is "invalid" if it's present (non-blank) but can't be read as the expected
    type — e.g. "ABC"/"12A" in a Revenue column. Blanks are left to drop_missing; this
    targets bad DATA, not missing data.
    """
    columns = op.get("columns") or []
    if not columns:
        raise OperationError("Which column should I check for invalid values?")
    _require_columns(df, columns)
    kind = (op.get("data_type") or "number").lower()
    if kind not in {"number", "date"}:  # never silently guess a type
        raise OperationError(
            f"I can't check for invalid '{kind}' values — I can check numbers or dates."
        )

    invalid = pd.Series(False, index=df.index)
    examples: list[str] = []
    for col in columns:
        s = df[col]
        blank = _blank_mask(s)
        parsed = _to_datetime(s) if kind == "date" else pd.to_numeric(s, errors="coerce")
        bad = parsed.isna() & ~blank
        examples += list(dict.fromkeys(s[bad].astype(str)))
        invalid = invalid | bad

    before = len(df)
    df = df[~invalid].reset_index(drop=True)
    removed = before - len(df)
    where = ", ".join(columns)
    if removed == 0:
        return df, f"No invalid {kind} values found in {where}."
    ex = ", ".join(f"'{v}'" for v in list(dict.fromkeys(examples))[:5])
    return df, (
        f"Removed {removed} row{'s' if removed != 1 else ''} where {where} "
        f"wasn't a valid {kind}" + (f" (e.g. {ex})" if ex else "") + "."
    )


def _trim(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Clean whitespace in text cells: strip leading/trailing spaces and collapse
    internal runs to a single space (Excel TRIM). Numbers/dates/blanks are untouched.
    Defaults to every text column when none are given."""
    requested = op.get("columns")
    if requested:
        _require_columns(df, requested)
        columns = requested
    else:
        columns = [c for c in df.columns if df[c].dtype == object or str(df[c].dtype) == "string"]

    df = df.copy()
    changed = 0
    for col in columns:
        series = df[col]
        if not (series.dtype == object or str(series.dtype) == "string"):
            continue  # never coerce numeric/date columns to text

        def clean(v):
            return re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v

        new = series.map(clean)
        changed += sum(1 for a, b in zip(series, new) if isinstance(a, str) and a != b)
        df[col] = new

    if changed == 0:
        return df, "No extra spaces found to trim."
    return df, f"Trimmed extra spaces in {changed} cell{'s' if changed != 1 else ''}."


def _coerce_like(series: pd.Series, value):
    """Coerce an incoming (string) edit to fit the column's kind, so a numeric column
    stays numeric and a date column stays dates (keeps formulas/formatting working).
    Blank → NaN; otherwise falls back to the raw string."""
    if value is None:
        return float("nan")
    s = str(value).strip()
    if s == "":
        return float("nan")
    if pd.api.types.is_datetime64_any_dtype(series):
        dt = _to_datetime(pd.Series([s])).iloc[0]
        if pd.notna(dt):
            return dt
    elif pd.api.types.is_numeric_dtype(series) or _is_numeric_like(series):
        num = pd.to_numeric(s, errors="coerce")
        if pd.notna(num):
            f = float(num)
            return int(f) if f.is_integer() else f
    return s


def _set_cells(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Apply manual cell edits from the grid. `edits` is a list of
    {row, column, value} where `row` is the 0-based position in the table. Values are
    coerced to the column's type; rows outside the table are skipped (the preview only
    shows a sample). This op is built by the UI from the user's edits — the AI never
    emits it — so it runs straight through /execute."""
    edits = op.get("edits") or []
    if not isinstance(edits, list) or not edits:
        raise OperationError("No cell edits were provided.")

    # Validate columns up front so a typo changes nothing.
    wanted = [e.get("column") for e in edits if isinstance(e, dict) and e.get("column")]
    _require_columns(df, list(dict.fromkeys(wanted)))

    out = df.copy()
    changed = 0
    for e in edits:
        if not isinstance(e, dict):
            continue
        col = e.get("column")
        row = e.get("row")
        if col not in out.columns:
            continue
        if not isinstance(row, int) or isinstance(row, bool) or row < 0 or row >= len(out):
            continue  # row is outside the visible sample — skip silently
        loc = out.columns.get_loc(col)
        value = _coerce_like(out[col], e.get("value"))
        try:
            out.iat[row, loc] = value
        except (ValueError, TypeError):
            # dtype clash (e.g. NaN into an int column) — relax the column to object
            out[col] = out[col].astype(object)
            out.iat[row, loc] = value
        changed += 1

    if changed == 0:
        return out, "No cells were updated (those rows are outside the preview)."
    return out, f"Updated {changed} cell{'s' if changed != 1 else ''}."


def _is_numeric_like(series: pd.Series) -> bool:
    """True if every non-blank value is a number (numeric dtype or numbers-as-text)."""
    nums = pd.to_numeric(series, errors="coerce")
    nonblank = ~_blank_mask(series)
    return int(nonblank.sum()) == 0 or bool(nums[nonblank].notna().all())


# Excel functions supported for the in-app preview value (the live formula can use any
# Excel function — these are the ones we also compute a preview for).
_FORMULA_FUNCS = {"SUM", "AVERAGE", "AVG", "MEAN", "MIN", "MAX", "ROUND", "ABS", "INT", "SQRT", "IF"}
_ARITH_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)


def _resolve_sheet_name(available, want: str) -> str | None:
    """Find the real sheet key for a user-facing name. Multi-file loads prefix sheets
    ('report.xlsx - Prices') and saved workbooks may truncate titles to 31 chars, but
    users (and the Brain) say 'Prices' — match exactly first, then by the ' - ' tail.
    Returns None unless the match is unambiguous."""
    names = list(available)
    low = want.strip().lower()
    exact = [n for n in names if str(n).strip().lower() == low]
    if len(exact) == 1:
        return exact[0]
    tails = [n for n in names if str(n).split(" - ")[-1].strip().lower() == low]
    if len(tails) == 1:
        return tails[0]
    # Tail-vs-tail: the caller may hold a FULL table name ('file - Prices') while the
    # workbook tab was stem-truncated ('file_trunc - Prices') — compare the sheet parts.
    want_tail = low.split(" - ")[-1].strip()
    tt = [n for n in names if str(n).split(" - ")[-1].strip().lower() == want_tail]
    if len(tt) == 1:
        return tt[0]
    return None


def _parse_ref(inner: str) -> tuple[str | None, str, bool]:
    """Decode a {placeholder}'s inner text -> (sheet, column, is_range).

    Grammar (Phase 1.1):  {Col} = this row's cell · {Col:} = the column's data range ·
    {Sheet.Col:} = a range on another sheet (sheet-qualified refs are ranges only)."""
    text = inner.strip()
    is_range = text.endswith(":")
    if is_range:
        text = text[:-1].strip()
    sheet = None
    if is_range and "." in text:
        sheet, text = text.split(".", 1)
        sheet, text = sheet.strip(), text.strip()
    return sheet, text, is_range


def _eval_formula(formula: str, df: pd.DataFrame, tables: dict | None = None,
                  notes: list | None = None, extra: dict | None = None):
    """Safely evaluate an Excel-ish per-row formula over the DataFrame's columns.

    Uses Python's ast with a strict whitelist (no eval/exec) so it can't run arbitrary
    code. Supports + - * / % ** & , parentheses, comparisons, AND/OR, string literals,
    TRUE/FALSE, and the functions in _FORMULA_FUNCS + the Phase-1.1 registry. {Column}
    placeholders map to row Series; {Column:} / {Sheet.Column:} map to _Range args for
    range-taking functions (SUMIF, XLOOKUP, RANK, UNIQUE, ...)."""
    if len(formula) > 2000:  # guard against pathological/deeply-nested expressions
        raise OperationError("That formula is too long — please simplify it.")

    colmap: dict[str, str] = {}

    def repl(m):
        c = m.group(1)
        colmap.setdefault(c, f"_c{len(colmap)}_")
        return colmap[c]

    safe = _PLACEHOLDER.sub(repl, formula)
    safe = safe.replace("<>", "!=")  # Excel not-equal -> Python
    safe = re.sub(r"(?<![<>=!])=(?!=)", "==", safe)  # Excel '=' equality -> '=='

    env: dict[str, object] = {}
    for ref, ident in colmap.items():
        sheet, col, is_range = _parse_ref(ref)
        if sheet is None and extra and col in extra:
            # A caller-supplied scalar binding (Goal Seek's {var} unknown).
            env[ident] = extra[col]
            continue
        if sheet is not None:
            real = _resolve_sheet_name((tables or {}).keys(), sheet)
            src = (tables or {}).get(real) if real else None
            if src is None:
                names = ", ".join((tables or {}).keys()) or "none"
                raise OperationError(
                    f"#REF!: there's no sheet called '{sheet}' (available: {names})."
                )
            if col not in src.columns:
                raise OperationError(
                    f"#REF!: sheet '{sheet}' has no column '{col}' "
                    f"(it has: {', '.join(map(str, src.columns))})."
                )
            env[ident] = _Range(src[col].reset_index(drop=True), sheet, col)
        elif is_range:
            if col not in df.columns:
                raise OperationError(f"#REF!: I couldn't find the column '{col}'.")
            env[ident] = _Range(df[col], None, col)
        else:
            env[ident] = (
                pd.to_numeric(df[col], errors="coerce") if _is_numeric_like(df[col]) else df[col]
            )
    # Evaluation context shared with the function registry (row count, index, soft notes).
    env["__ctx__"] = {"n": len(df), "index": df.index, "notes": notes if notes is not None else []}

    try:
        tree = ast.parse(safe, mode="eval")
    except SyntaxError as exc:
        raise OperationError(f"I couldn't parse the formula '{formula}'.") from exc
    return _ev(tree.body, env, len(df), df.index)


def _unrange(v):
    """Ranges may be compared/combined directly (FILTER({Price:}, {Region:}="North")) —
    operators see the underlying Series; only functions care about range-ness."""
    return v.series if isinstance(v, _Range) else v


def _ev(node, env, n, index):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        upper = node.id.upper()
        if upper == "TRUE":
            return True
        if upper == "FALSE":
            return False
        raise OperationError(f"Unknown name in formula: '{node.id}'.")
    if isinstance(node, ast.UnaryOp):
        v = _unrange(_ev(node.operand, env, n, index))
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        raise OperationError("Unsupported operator in formula.")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitAnd):
        # Excel's & is text concatenation.
        def txt(v):
            v = _unrange(v)
            if isinstance(v, pd.Series):
                if pd.api.types.is_numeric_dtype(v):
                    return v.map(lambda x: "" if pd.isna(x)
                                 else (str(int(x)) if float(x).is_integer() else str(x)))
                return v.astype("string").fillna("")
            return "" if v is None else str(v)
        left = txt(_ev(node.left, env, n, index))
        right = txt(_ev(node.right, env, n, index))
        return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ARITH_OPS):
        left = _unrange(_ev(node.left, env, n, index))
        right = _unrange(_ev(node.right, env, n, index))
        for operand in (left, right):
            if isinstance(operand, pd.Series) and operand.dtype == object:
                raise OperationError(
                    "A formula column needs numeric columns (one referenced column is text)."
                )
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.Pow):
            return _safe_pow(left, right)
        return left % right
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left = _unrange(_ev(node.left, env, n, index))
        right = _unrange(_ev(node.comparators[0], env, n, index))
        # Excel compares text case-insensitively ({Region:}="north" matches "North").
        if isinstance(left, pd.Series) and isinstance(right, str):
            left = left.astype("string").str.strip().str.lower()
            right = right.strip().lower()
        op = node.ops[0]
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
    if isinstance(node, ast.BoolOp):
        vals = [_unrange(_ev(v, env, n, index)) for v in node.values]
        out = vals[0]
        for v in vals[1:]:
            out = (out & v) if isinstance(node.op, ast.And) else (out | v)
        return out
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fname = node.func.id.upper()
        args = [_ev(a, env, n, index) for a in node.args]
        # Legacy row-wise functions keep their exact pre-1.1 behaviour — but only for
        # row arguments: SUM({Price:}) with a RANGE is a true whole-column aggregate
        # and belongs to the registry (row-wise SUM of a range would return the column
        # itself, silently wrong).
        has_range = any(isinstance(a, _Range) for a in args)
        if fname in _FORMULA_FUNCS and not has_range:
            return _apply_formula_func(fname, [_unrange(a) for a in args], n, index)
        # …the Phase-1.1 registry handles the wider families (ranges stay wrapped)…
        if fname in _REGISTRY_FUNCS:
            return _apply_registry_func(fname, args, env["__ctx__"])
        # …and known-but-unsupported functions get an honest, useful redirect.
        if fname in REDIRECTS:
            raise OperationError(f"{fname}() isn't generated here — {REDIRECTS[fname]}.")
        close = difflib.get_close_matches(fname, sorted(_REGISTRY_FUNCS | _FORMULA_FUNCS), n=1, cutoff=0.75)
        hint = f" Did you mean {close[0]}()?" if close else ""
        raise OperationError(f"#NAME?: the function {node.func.id}() isn't supported yet.{hint}")
    raise OperationError("That formula uses something I can't evaluate.")


_MAX_EXPONENT = 100  # cap powers so a formula can't DoS the process with giant numbers


def _safe_pow(left, right):
    """Exponentiation that can't hang or crash the process.

    `2 ** 100000` would build a million-digit integer (or raise OverflowError when
    converting to float). We reject huge exponents and compute in float64 so an
    overflow becomes inf, not a frozen process.
    """
    exp = right if isinstance(right, pd.Series) else pd.Series([right])
    exp_num = pd.to_numeric(exp, errors="coerce")
    if bool((exp_num.abs() > _MAX_EXPONENT).any()):
        raise OperationError(f"I can only raise to a power up to {_MAX_EXPONENT}.")
    base = left.astype("float64") if isinstance(left, pd.Series) else np.float64(left)
    with np.errstate(over="ignore", invalid="ignore"):
        return np.power(base, right)


def _series(v, n, index):
    return v if isinstance(v, pd.Series) else pd.Series([v] * n, index=index)


def _apply_formula_func(fname, args, n, index):
    if fname == "IF":
        cond = _series(args[0], n, index)
        a = args[1]
        b = args[2] if len(args) > 2 else None
        return pd.Series(np.where(cond.to_numpy(dtype=bool), a, b), index=index)
    if fname == "ABS":
        return _series(args[0], n, index).abs()
    if fname == "SQRT":
        return _series(args[0], n, index) ** 0.5
    if fname == "INT":
        return np.floor(_series(args[0], n, index))
    if fname == "ROUND":
        digits = int(args[1]) if len(args) > 1 else 0
        return _series(args[0], n, index).round(digits)
    cols = [_series(a, n, index) for a in args]
    if fname == "SUM":
        out = cols[0].copy()
        for c in cols[1:]:
            out = out + c
        return out
    if fname in ("AVERAGE", "AVG", "MEAN"):
        out = cols[0].copy()
        for c in cols[1:]:
            out = out + c
        return out / len(cols)
    if fname == "MIN":
        out = cols[0]
        for c in cols[1:]:
            out = np.minimum(out, c)
        return pd.Series(out, index=index)
    if fname == "MAX":
        out = cols[0]
        for c in cols[1:]:
            out = np.maximum(out, c)
        return pd.Series(out, index=index)
    raise OperationError(f"The function {fname}() isn't supported yet.")


# --- Self-correction loop for generated formulas (Phase 3.6) -------------------
# When a formula would yield an Excel error, we try ONE targeted repair per error
# class, then re-evaluate. The loop is bounded (each fix removes its own trigger, so
# it converges) — if a class can't be repaired we raise a clear explanation instead
# of looping. Full Excel error taxonomy (Phase 3.4) and how Sumio handles each:
#   #REF!    a referenced column doesn't exist          -> REPAIR: remap to the closest real one
#   #VALUE!  arithmetic hits text                        -> REPAIR: coerce numbers-from-text (else EXPLAIN)
#   #DIV/0!  a division hit a zero denominator           -> REPAIR: blank those rows (+ guard the saved formula)
#   #NUM!    invalid math (root of a negative, log<=0,    -> REPAIR: blank only rows whose inputs were
#            overflow)                                          present (blank-propagation is NOT an error)
#   #NAME?   an unknown/unsupported function              -> EXPLAIN (redirect to a supported form; never loop)
#   #N/A     a lookup found no match                      -> PREVENTED: lookup writes "Not found", never #N/A
#   #NULL!   two ranges that don't intersect              -> NOT PRODUCIBLE: Sumio has no range-intersection op
#   #SPILL!  a dynamic array can't spill                  -> PREVENTED: the live-pivot writer clears the anchor
#   #CALC!   a dynamic-array calculation error            -> NOT PRODUCIBLE: values are computed in pandas
_MAX_FORMULA_REPAIRS = 6


def _best_column_match(name: str, columns: list[str]) -> str | None:
    """The real column a (mistyped) reference most likely meant, or None.

    Tries case/space/punctuation-insensitive equality first, then a conservative
    fuzzy match so genuine typos are fixed but unrelated names are NOT."""
    target = _NON_ALNUM.sub("", name.lower())
    if not target:
        return None
    for c in columns:
        if _NON_ALNUM.sub("", c.lower()) == target:
            return c
    # Fuzzy: high cutoff so "Reveue"->"Revenue" matches but "Nope"->"Price" does not.
    matches = difflib.get_close_matches(name.lower(), [c.lower() for c in columns], n=1, cutoff=0.82)
    if matches:
        for c in columns:
            if c.lower() == matches[0]:
                return c
    return None


def _guard_division(formula: str) -> str | None:
    """If `formula` is a single top-level division `NUM / DEN`, return an Excel-safe
    guarded form `IF((DEN)=0, "", (NUM)/(DEN))` so the saved .xlsx no longer shows
    #DIV/0!. Returns None when there isn't exactly one top-level '/' (e.g. the slash
    is inside ROUND(...) or there are several) — the value-level blanking still applies."""
    depth = brace = count = slash_at = 0
    slash_at = -1
    for i, ch in enumerate(formula):
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
        elif brace == 0:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "/" and depth == 0:
                count += 1
                slash_at = i
    if count != 1 or slash_at <= 0:
        return None
    num = formula[:slash_at].strip()
    den = formula[slash_at + 1:].strip()
    if not num or not den:
        return None
    return f'IF(({den})=0, "", ({num})/({den}))'


def _compute_formula_self_correcting(
    df: pd.DataFrame, name: str, formula: str,
    tables: dict | None = None, notes: list | None = None,
) -> tuple[pd.Series, str, str, list[str]]:
    """Evaluate `formula`, auto-repairing #REF!/#VALUE!/#DIV/0! where possible.

    Returns (result, directive_formula, display_formula, repairs). `directive_formula`
    is what gets written to the .xlsx (may be a div-guarded form); `display_formula`
    is the human-readable corrected formula for the note. Raises OperationError with a
    plain explanation when an error class can't be repaired (never loops forever)."""
    work_df = df
    work_formula = formula
    repairs: list[str] = []
    value_repaired = False

    for _ in range(_MAX_FORMULA_REPAIRS):
        # Decode refs with the Phase-1.1 grammar. Same-sheet refs (row or {Col:} range)
        # are repairable here; sheet-qualified refs are validated inside _eval_formula
        # with their own clear #REF! messages (no cross-sheet fuzzy repair).
        raw_refs = _PLACEHOLDER.findall(work_formula)
        parsed = [(r, *_parse_ref(r)) for r in raw_refs]  # (raw, sheet, col, is_range)
        referenced = [col for _, sheet, col, _ in parsed if sheet is None]
        row_refs = [col for _, sheet, col, is_range in parsed if sheet is None and not is_range]

        # ---- #REF! : a referenced same-sheet column doesn't exist ----
        missing = [c for c in referenced if c not in work_df.columns]
        if missing:
            fixes: dict[str, str] = {}
            for bad in missing:
                good = _best_column_match(bad, list(work_df.columns))
                if good and good != bad:
                    fixes[bad] = good
            unresolved = [c for c in missing if c not in fixes]
            if unresolved:
                raise OperationError(
                    f"#REF!: I couldn't find the column"
                    f"{'s' if len(unresolved) != 1 else ''} {', '.join(unresolved)} "
                    f"used in '{name}'. Available columns: {', '.join(work_df.columns)}."
                )
            for bad, good in fixes.items():
                # Repair both {Bad} and {Bad:} forms, keeping the range suffix.
                work_formula = re.sub(
                    r"\{" + re.escape(bad) + r"(:?)\}",
                    lambda m: "{" + good + m.group(1) + "}",
                    work_formula,
                )
                repairs.append(f"#REF!: replaced missing {{{bad}}} with the closest column {{{good}}}")
            continue  # re-check with corrected references

        advanced = bool(re.search(r"[A-Za-z_]\w*\s*\(", work_formula)) or any(c in work_formula for c in "<>=")

        # ---- #VALUE! (plain arithmetic on text) : coerce numbers-from-text ----
        if not advanced:
            problem = [c for c in row_refs if not _is_numeric_like(work_df[c])]
            if problem:
                fixable, unfixable = [], []
                for c in problem:
                    num = pd.to_numeric(work_df[c], errors="coerce")
                    (fixable if num.notna().any() else unfixable).append((c, num))
                if unfixable:
                    cols = [c for c, _ in unfixable]
                    raise OperationError(
                        f"#VALUE!: {', '.join(cols)} "
                        f"{'aren’t' if len(cols) != 1 else 'isn’t'} numbers and can’t be "
                        f"converted, so '{name}' can’t be calculated. "
                        "A formula column needs numeric columns."
                    )
                work_df = work_df.copy()
                for c, num in fixable:
                    work_df[c] = num
                repairs.append(
                    "#VALUE!: converted text to numbers in "
                    + ", ".join(c for c, _ in fixable)
                    + " (blanks where a value wasn’t a number)"
                )
                continue

        # ---- evaluate ----
        try:
            result = _eval_formula(work_formula, work_df, tables, notes)
        except OperationError as exc:
            msg = str(exc)
            # #VALUE! inside an advanced formula (arithmetic hit a text column)
            if "text" in msg.lower() and not value_repaired:
                coerced = []
                tmp = work_df.copy()
                for c in row_refs:
                    if not _is_numeric_like(tmp[c]):
                        num = pd.to_numeric(tmp[c], errors="coerce")
                        if num.notna().any():
                            tmp[c] = num
                            coerced.append(c)
                if coerced:
                    work_df = tmp
                    value_repaired = True
                    repairs.append(
                        "#VALUE!: converted text to numbers in " + ", ".join(coerced)
                    )
                    continue
            raise  # other / unrepairable error -> explain, don't loop
        except Exception as exc:
            raise OperationError(f"Couldn't compute '{name}' from '{formula}': {exc}") from exc

        # ---- #DIV/0! : a division produced infinity ----
        display_formula = work_formula
        directive_formula = work_formula
        divzero_mask = None
        if "/" in work_formula and isinstance(result, pd.Series):
            res_num = pd.to_numeric(result, errors="coerce")
            inf_mask = np.isinf(res_num)
            div0 = int(inf_mask.sum())
            if div0:
                divzero_mask = inf_mask.to_numpy()
                result = result.mask(divzero_mask)
                repairs.append(
                    f"#DIV/0!: blanked {div0} row{'s' if div0 != 1 else ''} that divide by zero"
                )
                guarded = _guard_division(work_formula)
                if guarded:
                    directive_formula = guarded

        # ---- #NUM! : a valid-number input produced an INVALID number (root of a
        # negative, log of <=0, overflow). Blank ONLY rows whose referenced inputs were
        # all PRESENT (so a blank cell propagating a blank isn't mistaken for an error)
        # and that weren't already a divide-by-zero. Skip when the formula is text-valued
        # (no finite numbers at all) so a text result isn't falsely flagged.
        if isinstance(result, pd.Series):
            rn = pd.to_numeric(result, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
            nonfinite = ~np.isfinite(rn)
            if divzero_mask is not None:
                nonfinite = nonfinite & ~divzero_mask
            refs_present = [c for c in row_refs if c in work_df.columns]
            if refs_present and nonfinite.any() and np.isfinite(rn).any():
                present = np.logical_and.reduce(
                    [~_blank_mask(work_df[c]).to_numpy() for c in refs_present]
                )
                num_mask = nonfinite & present
                n = int(num_mask.sum())
                if n:
                    result = result.mask(num_mask)
                    repairs.append(
                        f"#NUM!: blanked {n} row{'s' if n != 1 else ''} that produced an invalid "
                        "number (e.g. the square root of a negative, log of zero, or overflow)"
                    )
        return result, directive_formula, display_formula, repairs

    # Safety net — should never be reached (each repair removes its own trigger).
    raise OperationError(
        f"I couldn't safely compute '{name}' after several repair attempts — "
        "please simplify the formula."
    )


_FUNC_TOKEN = re.compile(r"([A-Za-z][A-Za-z0-9_.]*)\s*\(")


def _add_formula_column(
    df: pd.DataFrame, op: dict, tables: dict | None = None
) -> tuple[pd.DataFrame, str, dict | None]:
    name = (op.get("name") or "").strip()
    formula = op.get("formula") or ""
    if not name:
        raise OperationError("A new formula column needs a name.")
    if not formula:
        raise OperationError(f"No formula provided for column '{name}'.")

    # 1.8-e: don't silently overwrite an existing column.
    if name in df.columns and not op.get("overwrite"):
        raise OperationError(
            f"A column called '{name}' already exists. Use a different name, "
            "or confirm you want to overwrite it."
        )

    # Compute with the self-correcting evaluator: it detects #REF!/#VALUE!/#DIV/0!
    # and auto-repairs where it can, or raises a plain explanation when it can't.
    soft_notes: list[str] = []
    result, directive_formula, display_formula, repairs = _compute_formula_self_correcting(
        df, name, formula, tables, soft_notes
    )

    # Dynamic-array (spill) results are shorter/longer than the frame: pad or trim the
    # PREVIEW to the frame, and mark the directive so the serializer writes ONE spilling
    # formula (Excel fills the rest) instead of copying it down every row.
    spill = isinstance(result, _Spill)
    df = df.copy()
    if spill:
        if len(result) > len(df):
            soft_notes.append(
                f"the formula spills {len(result):,} values but the sheet has "
                f"{len(df):,} data rows — the preview shows the first {len(df):,}; "
                "in Excel the live formula spills the full set"
            )
        vals = list(result)[: len(df)]
        vals += [np.nan] * (len(df) - len(vals))
        df[name] = vals
    else:
        df[name] = result.values if isinstance(result, pd.Series) else result

    # Emit a directive so the saved .xlsx gets a LIVE Excel formula (e.g. =B2*C2) — the
    # serializer fills in real cell references from the final layout. When a division was
    # guarded, the directive carries the safe IF(...) form so Excel won't show #DIV/0!.
    directive = {"type": "formula", "column": name, "formula": directive_formula, "spill": spill}
    # Cross-sheet ranges ({Prices.Unit_Price:}) need those sheets IN the output workbook
    # for the live formula to reference — attach their data so the serializer can write
    # any that are missing (same pattern as the lookup op's source_df).
    source_sheets: dict = {}
    for raw in _PLACEHOLDER.findall(directive_formula):
        ref_sheet, _, _ = _parse_ref(raw)
        if ref_sheet and ref_sheet not in source_sheets:
            real = _resolve_sheet_name((tables or {}).keys(), ref_sheet)
            if real is not None:
                source_sheets[ref_sheet] = tables[real]
    if source_sheets:
        directive["source_sheets"] = source_sheets
    note = f"Added column '{name}' = {display_formula}."
    if repairs:
        note += " Auto-corrected: " + "; ".join(repairs) + "."

    # Version awareness (Phase 1.1): name the minimum Excel for modern functions. The
    # preview values are Sumio-computed either way, so the data is usable everywhere.
    used = {t.upper() for t in _FUNC_TOKEN.findall(directive_formula)}
    needs = {f: v for f, v in M365_FUNCS.items() if f in used}
    if needs:
        reqs = "; ".join(f"{f} needs {v}" for f, v in sorted(needs.items()))
        note += (
            f" Note: {reqs} — older Excel shows #NAME? for the live formula, but the "
            "computed values are shown in the preview and saved with the file."
        )
    for extra in soft_notes:
        note += f" Note: {extra}."
    return df, note, directive


def _flag_missing(df: pd.DataFrame, op: dict) -> tuple[str, dict]:
    """Record that blank cells should be highlighted (without changing the data).
    Applied to the saved .xlsx by the serializer."""
    columns = op.get("columns") or list(df.columns)
    _require_columns(df, columns)
    total = int(sum(int(_blank_mask(df[c]).sum()) for c in columns))
    directive = {"type": "highlight", "columns": columns}
    where = ", ".join(op.get("columns")) if op.get("columns") else "the sheet"
    if total == 0:
        return f"No missing values found in {where}.", directive
    note = (
        f"Highlighted {total} blank cell{'s' if total != 1 else ''} in {where} "
        "(data unchanged; shown in .xlsx downloads)."
    )
    return note, directive


def _lookup(df: pd.DataFrame, op: dict, sheets: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, str, dict]:
    key_column = op.get("key_column")
    source_sheet = op.get("source_sheet")
    source_key_column = op.get("source_key_column")
    return_column = op.get("return_column")
    new_column = op.get("new_column") or return_column or "Lookup"

    if not all([key_column, source_sheet, source_key_column, return_column]):
        raise OperationError(
            "Lookup needs: the key column here, the source sheet, the key column "
            "in that sheet, and the column to bring back."
        )
    _require_columns(df, [key_column])

    if source_sheet not in sheets:
        available = ", ".join(sheets) or "(none)"
        raise OperationError(
            f"I don't see a sheet named '{source_sheet}'. Available sheets: {available}."
        )
    source = sheets[source_sheet]
    missing = [c for c in (source_key_column, return_column) if c not in source.columns]
    if missing:
        raise OperationError(
            f"Sheet '{source_sheet}' has no column(s): {', '.join(missing)}. "
            f"It has: {', '.join(map(str, source.columns))}."
        )

    # First match wins for duplicate keys in the source.
    deduped = source.drop_duplicates(subset=[source_key_column], keep="first")
    had_dupes = len(deduped) != len(source)
    # Keys are matched the "dedupe-style" way: trimmed, case-insensitive, and with
    # 123 == "123" (see _norm_key). Drop blank source keys so empty cells never match.
    mapping = {
        k: v
        for k, v in zip(_norm_key(deduped[source_key_column]), deduped[return_column])
        if k is not None
    }

    norm_df_keys = _norm_key(df[key_column]).reset_index(drop=True)
    fetched = norm_df_keys.map(mapping)  # positional (0..n-1) after the reset above

    # How many matched ONLY because we normalized? (exact case/space/type-sensitive
    # match would have missed them.) Used to honestly flag the behavior to the user.
    exact_keys = set(source[source_key_column].dropna())
    exact_matched = int(df[key_column].isin(exact_keys).sum())

    # --- Fuzzy / typo-tolerant fallback (Phase 3.1) --------------------------------
    # For keys that still didn't match, look for a CLOSE source key (edit-distance) —
    # so "Jon Smith" finds "John Smith". Only high-confidence matches, and each fuzzy
    # hit is written as a STATIC value (a typo match can't be reproduced by a live
    # Excel formula) and honestly reported so the user can verify it.
    static_overrides: dict[int, object] = {}
    fuzzy_examples: list[str] = []
    fuzzy_count = 0
    source_norm_list = list(mapping.keys())
    orig_keys = df[key_column].reset_index(drop=True)
    unmatched_positions = [p for p in range(len(fetched))
                           if pd.isna(fetched.iloc[p]) and norm_df_keys.iloc[p] is not None]
    # Guard against an O(rows × keys) blow-up on large data.
    if unmatched_positions and len(source_norm_list) <= 5000 and len(unmatched_positions) <= 5000:
        norm_to_orig: dict[str, object] = {}
        for orig, nk in zip(deduped[source_key_column], _norm_key(deduped[source_key_column])):
            if nk is not None:
                norm_to_orig.setdefault(nk, orig)
        for p in unmatched_positions:
            close = difflib.get_close_matches(norm_df_keys.iloc[p], source_norm_list, n=1, cutoff=0.85)
            if close:
                val = mapping[close[0]]
                fetched.iloc[p] = val
                static_overrides[p] = val
                fuzzy_count += 1
                if len(fuzzy_examples) < 3:
                    fuzzy_examples.append(f"'{orig_keys.iloc[p]}'→'{norm_to_orig.get(close[0], close[0])}'")

    matched = int(fetched.notna().sum())
    df = df.copy()
    df[new_column] = fetched.where(fetched.notna(), "Not found").values  # positional assign

    note = (
        f"Looked up '{return_column}' from sheet '{source_sheet}' by '{key_column}' "
        f"into a new column '{new_column}' ({matched} of {len(df)} rows matched)."
    )
    if had_dupes:
        note += " The source had duplicate keys, so I used the first match."
    normalized_extra = matched - exact_matched - fuzzy_count
    if normalized_extra > 0:
        note += (
            f" {normalized_extra} row(s) matched only after ignoring case, "
            "surrounding spaces, or number-vs-text differences."
        )
    if fuzzy_count:
        eg = " (e.g. " + ", ".join(fuzzy_examples) + ")" if fuzzy_examples else ""
        note += (
            f" {fuzzy_count} row(s) matched by CLOSE SIMILARITY — likely typos{eg}. "
            "These are written as fixed values (not live formulas), so please verify them."
        )

    # We compute values now (for preview/JSON), AND emit a directive so the saved
    # .xlsx writes the source as its own sheet and a LIVE lookup formula into the
    # column. We pass the normalized source keys so the live formula reproduces the
    # SAME matches as the preview (same trim/case/type rules), not Excel's stricter
    # exact match.
    directive = {
        "type": "lookup",
        "new_column": new_column,
        "key_column": key_column,
        "source_name": source_sheet,
        "source_df": source,
        "source_key_column": source_key_column,
        "return_column": return_column,
        "source_norm_keys": [("" if k is None else k) for k in _norm_key(source[source_key_column])],
        # Phase 3.1: rows that matched only by fuzzy similarity are written as STATIC
        # values (a typo match can't be an Excel formula) — {positional row -> value}.
        "static_overrides": static_overrides,
    }
    return df, note, directive


def _aggregate(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    raw = (op.get("agg_func") or "").lower()
    func = {"average": "mean", "avg": "mean"}.get(raw, raw)
    if func not in {"sum", "mean", "count", "min", "max"}:
        raise OperationError(
            f"I can compute sum, average, count, min, or max — not '{raw}'."
        )

    pretty = {"mean": "average"}.get(func, func)  # user-facing word
    column = op.get("agg_column")
    group_by = op.get("group_by") or []
    count_value = op.get("count_value")
    if group_by:
        _require_columns(df, group_by)
    if func != "count" and not column:
        raise OperationError(f"Which column should I take the {pretty} of?")
    if count_value is not None and not column:
        raise OperationError("To count a specific value, tell me which column to look in.")
    if column:
        _require_columns(df, [column])

    work = df.copy()
    blanks_ignored = 0
    bad_values: list[str] = []  # non-blank cells that aren't numbers (e.g. "ABC")
    # sum/average/min/max need numbers; coerce and verify the column is numeric.
    if func in {"sum", "mean", "min", "max"} and column:
        nums = pd.to_numeric(work[column], errors="coerce")
        if nums.notna().sum() == 0:
            verb = {"sum": "sum", "mean": "average"}.get(func, f"take the {func} of")
            raise OperationError(
                f"Can't {verb} '{column}' — it looks like text, not numbers."
            )
        blanks_ignored = int(nums.isna().sum())  # blanks + any non-numeric cells
        # Names of non-blank values that aren't numbers, so the note can call them out.
        offenders = work[column][nums.isna() & ~_blank_mask(work[column])]
        bad_values = list(dict.fromkeys(offenders.astype(str)))[:5]
        work[column] = nums

    # Counting a specific value: keep only the cells that match it (case/space-insensitive).
    if func == "count" and count_value is not None:
        target = _norm_key(pd.Series([count_value])).iloc[0]
        match_mask = _norm_key(work[column]) == target

    if func == "count" and count_value is None and column:
        blanks_ignored = int(_blank_mask(work[column]).sum())

    label = _agg_label(func, column, count_value)

    if group_by:
        if func == "count" and count_value is not None:
            matched = work[match_mask]
            result = matched.groupby(group_by, dropna=False).size().reset_index(name=label)
        elif func == "count":
            result = work.groupby(group_by, dropna=False).size().reset_index(name=label)
        else:
            result = work.groupby(group_by, dropna=False)[column].agg(func).reset_index()
            result = result.rename(columns={column: label})
        for g in group_by:
            result[g] = result[g].where(result[g].notna(), "(blank)")
        note = (
            f"Computed {pretty}{_agg_of(column, count_value)} grouped by "
            f"{', '.join(group_by)} ({len(result)} group{'s' if len(result) != 1 else ''})."
        )
        note += _blanks_note(func, blanks_ignored, column, bad_values)
        return result, note

    if func == "count":
        if count_value is not None:
            value = int(match_mask.sum())
        elif column:
            value = int((~_blank_mask(work[column])).sum())
        else:
            value = int(len(work))
    else:
        value = work[column].agg(func)
        if pd.isna(value):  # every value was blank/non-numeric
            value = 0
    # A single-value aggregate ANSWERS the question without destroying the data:
    # report the value in the note and keep the (possibly filtered) rows as the result,
    # so "filter fraud, then average Amount" still hands back the fraud transactions.
    note = f"Computed {pretty}{_agg_of(column, count_value)}: {_fmt_num(value)}."
    note += _blanks_note(func, blanks_ignored, column, bad_values)
    return df, note


def _fmt_num(v):
    """Tidy a computed value for display (drop noisy float tails, keep ints clean)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return str(int(f))
    return f"{round(f, 2)}"


def _agg_label(func: str, column: str | None, count_value) -> str:
    if func == "count" and count_value is not None:
        return f"count_of_{count_value}"
    word = {"mean": "average"}.get(func, func)  # match the user-facing wording
    return f"{word}_of_{column}" if column else "count"


def _agg_of(column: str | None, count_value) -> str:
    if count_value is not None:
        return f" of '{count_value}' in '{column}'"
    return f" of '{column}'" if column else ""


def _blanks_note(func: str, blanks_ignored: int, column: str | None, bad_values: list[str] | None = None) -> str:
    """Say so when blank (or non-numeric) cells were left out of the calculation, and
    NAME any non-numeric values (e.g. 'ABC') so the user can spot bad data."""
    if not column or blanks_ignored <= 0:
        return ""
    kind = "blank" if func == "count" else "blank or non-numeric"
    note = f" (ignored {blanks_ignored} {kind} cell{'s' if blanks_ignored != 1 else ''}"
    if bad_values:
        shown = ", ".join(f"'{v}'" for v in bad_values)
        note += f" — including non-number{'s' if len(bad_values) != 1 else ''}: {shown}"
    return note + ")"


def _find_replace(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    find = op.get("find")
    if find is None or find == "":
        raise OperationError("Tell me what text to find.")
    replace = op.get("replace")
    if replace is None:
        replace = ""
    column = op.get("column")
    match_case = bool(op.get("match_case"))
    whole_cell = bool(op.get("whole_cell"))

    if column:
        _require_columns(df, [column])
        columns = [column]
    else:
        # Whole-sheet replace only touches text columns, so numbers aren't mangled.
        columns = [c for c in df.columns if df[c].dtype == object or str(df[c].dtype) == "string"]

    df = df.copy()
    pattern = re.compile(re.escape(find), 0 if match_case else re.IGNORECASE)
    total = 0
    for col in columns:
        changed = 0
        new_values = []
        for v in df[col].tolist():
            if v is None or (isinstance(v, float) and pd.isna(v)):
                new_values.append(v)
                continue
            s = str(v)
            if whole_cell:
                # Whole-cell match ignores surrounding whitespace, so "Mumbai " and
                # "Mumbai" are treated as the same cell (matches the app's trimmed
                # philosophy and the "Mumbai "-means-Mumbai use case).
                cell = s.strip()
                target = find.strip()
                if cell == target or (not match_case and cell.lower() == target.lower()):
                    new_values.append(replace)
                    changed += 1
                else:
                    new_values.append(v)
            else:
                new_s, n = pattern.subn(lambda _m: replace, s)
                if n > 0:
                    changed += 1
                    new_values.append(new_s)
                else:
                    new_values.append(v)
        if changed:
            df[col] = new_values
        total += changed

    if total == 0:
        return df, f"No cells matched '{find}', so nothing was replaced."
    return df, f"Replaced '{find}' with '{replace}' in {total} cell{'s' if total != 1 else ''}."


def _rename_columns(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    rename_from = op.get("rename_from") or []
    rename_to = op.get("rename_to") or []
    if not rename_from or len(rename_from) != len(rename_to):
        raise OperationError(
            "To rename, give the same number of old names and new names."
        )
    _require_columns(df, rename_from)

    mapping = dict(zip(rename_from, rename_to))
    df = df.rename(columns=mapping)
    pairs = ", ".join(f"'{a}' → '{b}'" for a, b in mapping.items())
    return df, f"Renamed {pairs}."


def _drop_columns(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    columns = op.get("columns") or []
    if not columns:
        raise OperationError("Which column(s) should I remove?")
    _require_columns(df, columns)
    df = df.drop(columns=columns)
    return df, f"Removed column{'s' if len(columns) != 1 else ''}: {', '.join(columns)}."


def _select_columns(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    columns = op.get("columns") or []
    if not columns:
        raise OperationError("Which column(s) should I keep?")
    _require_columns(df, columns)
    # Say what was DROPPED, not just what was kept: "keep these two" can quietly delete
    # eighteen others, and the user should see that number without counting headers.
    dropped = [str(c) for c in df.columns if c not in columns]
    df = df[columns]
    if not dropped:
        return df, f"Kept only: {', '.join(columns)} (nothing else to remove)."
    shown = ", ".join(dropped[:6]) + ("…" if len(dropped) > 6 else "")
    return df, (
        f"Kept only: {', '.join(columns)} — removed "
        f"{len(dropped)} column{'s' if len(dropped) != 1 else ''} ({shown})."
    )


def _format_mismatch(df: pd.DataFrame, columns: list[str], number_format: str) -> list[str]:
    """Columns whose actual contents don't match the requested format — e.g. a
    currency format on a text column, or a date format on free text. Used to warn
    the user (we still apply the format; it just won't display as intended).
    """
    bad: list[str] = []
    for col in columns:
        s = df[col]
        if len(s) == 0:
            continue
        if number_format in ("currency", "percent", "number"):
            if int(pd.to_numeric(s, errors="coerce").notna().sum()) == 0:
                bad.append(col)  # nothing numeric to format
        elif number_format == "date":
            if not pd.api.types.is_datetime64_any_dtype(s) and int(_to_datetime(s).notna().sum()) == 0:
                bad.append(col)  # nothing date-like to format
    return bad


def _format_cells(df: pd.DataFrame, op: dict) -> tuple[str, dict]:
    """Records a formatting directive. Formatting doesn't change the data, so it's
    applied when the workbook is saved (see main._serialize)."""
    columns = op.get("format_columns") or []
    number_format = op.get("number_format")
    bold_header = bool(op.get("bold_header"))

    if columns:
        _require_columns(df, columns)
    if not columns and not bold_header:
        raise OperationError("Tell me which columns to format, or to bold the header.")
    if columns and not number_format:
        raise OperationError(
            "What format should I apply — currency, percent, number, or date?"
        )

    directive = {
        "type": "format",
        "columns": columns,
        "format": number_format,
        "decimals": op.get("decimals"),
        "currency_symbol": op.get("currency_symbol"),
        "date_format": op.get("date_format"),
        "bold_header": bold_header,
    }

    parts = []
    if columns and number_format:
        parts.append(f"formatted {', '.join(columns)} as {number_format}")
    if bold_header:
        parts.append("bolded the header row")
    note = "Applied formatting: " + " and ".join(parts) + ". (Formatting shows in .xlsx downloads.)"

    # 1.11-e: warn (don't crash) when a column's type doesn't match the format.
    mismatched = _format_mismatch(df, columns, number_format) if columns and number_format else []
    if mismatched:
        nice = {"currency": "numbers", "percent": "numbers", "number": "numbers", "date": "dates"}
        note += (
            f" Note: {', '.join(mismatched)} don't look like {nice.get(number_format, number_format)}, "
            f"so the {number_format} format may not show as expected — your values weren't changed."
        )
    return note, directive


# ---------------------------------------------------------------------------
# Phase 3.5 — Predictive analytics
# ---------------------------------------------------------------------------

_MIN_FORECAST_ROWS = 5     # linear regression needs at least this many points
_MIN_ANOMALY_ROWS = 5      # z-score / IQR need at least this many rows


def _confidence_band(pct: int) -> str:
    """A plain-language band for a 0-100 confidence score (Phase 3.10)."""
    if pct >= 70:
        return "high"
    if pct >= 40:
        return "moderate"
    return "low"


def _ols_forecast(
    x: np.ndarray, y: np.ndarray, x_new: np.ndarray, ci: float = 1.96
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pure-NumPy OLS linear regression + prediction intervals.

    Returns (y_hat, lower, upper) for `x_new`. Uses prediction interval
    (not just confidence interval) so individual future points are covered.
    """
    n = len(x)
    x_bar = x.mean()
    Sxx = float(np.sum((x - x_bar) ** 2))
    m = float(np.sum((x - x_bar) * (y - y.mean())) / Sxx) if Sxx > 0 else 0.0
    b = float(y.mean() - m * x_bar)

    y_fit = m * x + b
    sse = float(np.sum((y - y_fit) ** 2))
    # s² = MSE (residual variance); prediction SE adds 1 for a new observation
    s2 = sse / max(1, n - 2)

    y_hat = m * x_new + b
    se_pred = np.sqrt(s2 * (1 + 1 / n + (x_new - x_bar) ** 2 / max(Sxx, 1e-12)))
    margin = ci * se_pred

    return y_hat, y_hat - margin, y_hat + margin


def _r_squared(x: np.ndarray, y: np.ndarray) -> float:
    """Coefficient of determination for a simple linear fit of y on x (0..1).

    For simple linear regression R² is the square of Pearson's r. Returns 0.0 when
    either axis is constant (no trend can be measured)."""
    if len(x) < 2:
        return 0.0
    sx = float(np.std(x))
    sy = float(np.std(y))
    if sx < 1e-12 or sy < 1e-12:
        return 0.0
    r = float(np.corrcoef(x, y)[0, 1])
    if np.isnan(r):
        return 0.0
    return r * r


def _forecast(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Append forecast rows with 95 % prediction intervals (linear trend)."""
    value_cols = op.get("columns") or []
    if not value_cols:
        raise OperationError(
            "Which column should I forecast? Please name a numeric column."
        )
    _require_columns(df, value_cols)

    date_col = op.get("date_column")
    if date_col:
        _require_columns(df, [date_col])

    periods = int(op.get("count") or 3)
    if periods < 1 or periods > 100:
        raise OperationError(
            f"I can forecast 1–100 periods ahead, not {periods}."
        )
    period_unit = op.get("period_unit") or "period"

    # Build the numeric X-axis (row index 0, 1, 2, … or date ordinal)
    n = len(df)
    if n < _MIN_FORECAST_ROWS:
        raise OperationError(
            f"I need at least {_MIN_FORECAST_ROWS} data points to build a reliable forecast "
            f"— this table only has {n} row{'s' if n != 1 else ''}. "
            "Try a longer history or use what-if simulation for scenario planning."
        )

    if date_col:
        dates = _to_datetime(df[date_col])
        if dates.isna().all():
            raise OperationError(
                f"'{date_col}' doesn't look like a date column — I can't build a time axis."
            )
        x_hist = dates.dropna().map(lambda d: d.toordinal()).values.astype(float)
        last_date = dates.dropna().iloc[-1]
        # Infer step size from the median gap between consecutive dates
        if len(x_hist) >= 2:
            median_gap = float(np.median(np.diff(x_hist)))
        else:
            median_gap = 30.0
        x_new = np.array([last_date.toordinal() + median_gap * (i + 1) for i in range(periods)])
        # Generate human-readable future labels
        import datetime
        future_labels = []
        for i in range(periods):
            try:
                future_labels.append(
                    str(datetime.date.fromordinal(int(x_new[i])))
                )
            except (ValueError, OverflowError):
                future_labels.append(f"Period +{i+1}")
    else:
        x_hist = np.arange(n, dtype=float)
        x_new = np.arange(n, n + periods, dtype=float)
        future_labels = [f"{period_unit.capitalize()} +{i+1}" for i in range(periods)]

    # Forecast each value column
    result = df.copy()
    forecast_rows_data: dict[str, list] = {}
    if date_col:
        forecast_rows_data[date_col] = future_labels

    col_summaries = []
    weak_cols: list[str] = []
    r2_values: list[float] = []
    for col in value_cols:
        y_raw = pd.to_numeric(df[col], errors="coerce")
        y_valid = y_raw.dropna()
        if len(y_valid) < _MIN_FORECAST_ROWS:
            raise OperationError(
                f"'{col}' has only {len(y_valid)} non-blank numeric value(s) — "
                f"I need at least {_MIN_FORECAST_ROWS} to forecast reliably."
            )
        x_fit = x_hist[y_raw.notna().values]
        y_fit = y_valid.values.astype(float)
        y_hat, lower, upper = _ols_forecast(x_fit, y_fit, x_new)

        # Goodness of fit: R² of the historical linear fit (0 = no trend, 1 = perfect).
        # Honest signalling — a low R² means the straight-line forecast is unreliable.
        r2 = _r_squared(x_fit, y_fit)
        r2_values.append(r2)

        forecast_rows_data[f"{col}_Forecast"] = [round(float(v), 4) for v in y_hat]
        forecast_rows_data[f"{col}_Lower95"] = [round(float(v), 4) for v in lower]
        forecast_rows_data[f"{col}_Upper95"] = [round(float(v), 4) for v in upper]
        col_summaries.append(
            f"{col}: {y_hat[0]:,.2f} → {y_hat[-1]:,.2f} "
            f"(95% CI ±{float(np.mean(y_hat - lower)):,.2f}, R²={r2:.2f})"
        )
        if r2 < 0.30:
            weak_cols.append(col)

    # Build forecast rows (blank for columns not in the forecast set)
    forecast_df = pd.DataFrame(forecast_rows_data, index=range(n, n + periods))
    result = pd.concat([result, forecast_df], ignore_index=True)

    unit_label = f"{periods} {period_unit}{'s' if periods != 1 else ''}"
    note = (
        f"Forecast {unit_label} ahead: {'; '.join(col_summaries)}. "
        "Rows with _Forecast / _Lower95 / _Upper95 columns hold the projections."
    )

    # Confidence (3.10): driven by goodness-of-fit (weakest column) and sample size.
    overall_r2 = min(r2_values) if r2_values else 0.0
    n_factor = min(1.0, n / 12.0)  # ~12 points for full trust in a linear fit
    confidence = int(round(100 * overall_r2 * (0.6 + 0.4 * n_factor)))
    band = _confidence_band(confidence)
    note += f" Forecast confidence: {confidence}% ({band})."

    if weak_cols:
        note += (
            f" Caution: {', '.join(weak_cols)} shows little linear trend (low R²), "
            "so treat this projection as a rough guide — the confidence range is wide."
        )
    directive = {
        "type": "analysis", "kind": "forecast", "confidence": confidence,
        "band": band, "columns": value_cols,
        "detail": f"Linear trend over {n} data point{'s' if n != 1 else ''} (R²={overall_r2:.2f}).",
    }
    return result, note, directive


def _what_if(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict | None]:
    """Add a scenario column showing the impact of a hypothetical change.

    The LLM provides `column` (the base), `formula` (using {column} placeholders),
    and `name` (the scenario column label). We evaluate the formula, compute the
    delta, and add a summary note plus a structured before/after directive so the UI
    can show the scenario impact the same way it shows forecast/anomaly analyses.

    A what-if is EXACT deterministic arithmetic, not a prediction — so, unlike the
    forecast, it carries no "confidence %" (that would imply a false uncertainty).
    """
    col = op.get("column")
    formula = op.get("formula") or ""
    scenario_name = op.get("name") or (f"{col} (Scenario)" if col else "Scenario")

    if not col:
        raise OperationError(
            "Tell me which column to change — e.g., 'what if Price increases by 10%'."
        )
    _require_columns(df, [col])
    if not formula:
        raise OperationError(
            f"Tell me how '{col}' should change — e.g., '{{col}} * 1.1' for +10%."
        )

    scenario_vals = _eval_formula(formula, df)

    if scenario_name in df.columns:
        # overwrite if already present (re-running a what-if)
        result = df.copy()
        result[scenario_name] = scenario_vals
    else:
        result = df.copy()
        result[scenario_name] = scenario_vals

    # Summary: compare original vs scenario for numeric columns (the "before/after").
    orig_num = pd.to_numeric(df[col], errors="coerce")
    new_num = pd.to_numeric(result[scenario_name], errors="coerce")
    before = after = delta = pct = None
    if orig_num.notna().any() and new_num.notna().any():
        before = float(orig_num.sum())
        after = float(new_num.sum())
        delta = after - before
        if before != 0:
            pct = delta / abs(before) * 100
            note = (
                f"What-if scenario: if '{col}' follows '{formula}', total goes from "
                f"{before:,.2f} → {after:,.2f} ({pct:+.1f}%). "
                f"Scenario values are in '{scenario_name}'."
            )
        else:
            note = (
                f"What-if scenario added as '{scenario_name}' "
                f"(total: {after:,.2f})."
            )
    else:
        note = f"What-if scenario added as '{scenario_name}'."

    # Structured before/after directive (Phase 4.5) — parallels the forecast/anomaly
    # `analysis` blocks so /process surfaces the scenario impact uniformly. No confidence
    # key: the math is exact, and inventing a % would misrepresent it as a prediction.
    directive = {
        "type": "analysis", "kind": "what_if", "column": col,
        "scenario_column": scenario_name, "formula": formula,
        "before": None if before is None else round(before, 4),
        "after": None if after is None else round(after, 4),
        "delta": None if delta is None else round(delta, 4),
        "pct": None if pct is None else round(pct, 2),
        "detail": "Exact deterministic scenario — no forecast uncertainty.",
    }
    return result, note, directive


def _detect_anomalies(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str]:
    """Flag rows whose numeric values are unusually high or low.

    Adds 'Is_Anomaly' (bool) and 'Anomaly_Note' (reason) columns.
    Method: 'zscore' (default, |z| > threshold=3.0) or 'iqr' (default multiplier=1.5).
    """
    cols = op.get("columns") or []
    if not cols:
        # Default: all numeric columns
        cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        if not cols:
            # Try numeric-like text columns
            cols = [c for c in df.columns if _is_numeric_like(df[c])]
        if not cols:
            raise OperationError(
                "No numeric columns found to check for anomalies."
            )
    else:
        _require_columns(df, cols)

    n = len(df)
    if n < _MIN_ANOMALY_ROWS:
        raise OperationError(
            f"I need at least {_MIN_ANOMALY_ROWS} rows to detect anomalies reliably — "
            f"this table only has {n} row{'s' if n != 1 else ''}."
        )

    method = (op.get("anomaly_method") or "zscore").lower()
    if method not in ("zscore", "iqr"):
        method = "zscore"

    threshold = op.get("anomaly_threshold")
    if threshold is None:
        threshold = 3.0 if method == "zscore" else 1.5
    threshold = float(threshold)

    result = df.copy()
    is_anomaly = pd.Series(False, index=df.index)
    anomaly_notes: list[str] = [""] * len(df)

    flagged_cols = 0
    for col in cols:
        series = pd.to_numeric(df[col], errors="coerce")
        valid = series.dropna()
        if len(valid) < _MIN_ANOMALY_ROWS:
            continue

        flagged_cols += 1
        if method == "zscore":
            # Modified z-score (Iglewicz & Hoaglin 1993): uses median + MAD so that
            # extreme outliers don't inflate the variance and mask each other.
            median_ = float(valid.median())
            mad = float((valid - median_).abs().median())
            if mad < 1e-10:
                # Constant-ish column: fall back to mean/std
                std_ = float(valid.std())
                if std_ < 1e-10:
                    continue  # perfectly constant — no outliers
                z = (series - float(valid.mean())) / std_
            else:
                z = 0.6745 * (series - median_) / mad
            high_mask = z > threshold
            low_mask = z < -threshold
        else:  # iqr
            q1 = float(valid.quantile(0.25))
            q3 = float(valid.quantile(0.75))
            iqr = q3 - q1
            if iqr < 1e-10:
                continue  # no spread — no outliers
            fence_lo = q1 - threshold * iqr
            fence_hi = q3 + threshold * iqr
            high_mask = series > fence_hi
            low_mask = series < fence_lo

        for i in df.index[high_mask & series.notna()]:
            is_anomaly.at[i] = True
            reason = f"{col} is unusually HIGH ({series.at[i]:g})"
            anomaly_notes[i] = (anomaly_notes[i] + "; " + reason).lstrip("; ")

        for i in df.index[low_mask & series.notna()]:
            is_anomaly.at[i] = True
            reason = f"{col} is unusually LOW ({series.at[i]:g})"
            anomaly_notes[i] = (anomaly_notes[i] + "; " + reason).lstrip("; ")

    if flagged_cols == 0:
        raise OperationError(
            f"None of the selected columns ({', '.join(cols)}) had enough "
            "numeric data to check — they may be text or all blank."
        )

    result["Is_Anomaly"] = is_anomaly
    result["Anomaly_Note"] = anomaly_notes

    n_flagged = int(is_anomaly.sum())
    method_label = (
        f"z-score > {threshold}" if method == "zscore"
        else f"IQR × {threshold}"
    )

    # Confidence (3.10): more rows analysed → more trustworthy flags.
    confidence = 50 if n < 10 else (72 if n < 30 else 90)
    band = _confidence_band(confidence)
    directive = {
        "type": "analysis", "kind": "anomaly", "confidence": confidence, "band": band,
        "columns": cols,
        "detail": f"{method_label} over {n} row{'s' if n != 1 else ''}; {n_flagged} flagged.",
    }
    conf_note = f" Detection confidence: {confidence}% ({band}, based on {n} rows)."

    if n_flagged == 0:
        return result, (
            f"No anomalies found in {', '.join(cols)} ({method_label}). "
            "All values are within normal range." + conf_note
        ), directive
    return result, (
        f"Flagged {n_flagged} row{'s' if n_flagged != 1 else ''} as anomalies "
        f"in {', '.join(cols)} ({method_label}). "
        f"Check 'Is_Anomaly' and 'Anomaly_Note' columns for details." + conf_note
    ), directive
