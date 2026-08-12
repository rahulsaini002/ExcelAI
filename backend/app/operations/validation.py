"""Engine Phase 1.5 — data validation / dropdowns (Area 4).

One op -> a real openpyxl DataValidation in the saved file:
  validation_type  "list" | "whole" | "decimal" | "date" | "text_length" | "custom"
  columns          which column(s) get the rule
  allowed_values   for "list" — omit to DERIVE the dropdown from the column's own
                   distinct values ("add a dropdown of Regions")
  min_value/max_value  bounds for whole/decimal/date (ISO dates)/text_length
  formula          for "custom" — the {Col} grammar, rendered like CF formulas
  input_message / error_message / allow_blank

Honesty: validation only constrains FUTURE edits, so the note counts how many existing
cells already violate the rule and says the data itself is unchanged.
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError

TYPES = {"list", "whole", "decimal", "date", "text_length", "custom"}
MAX_DERIVED = 50  # a dropdown of more distinct values than this is a data problem


def data_validation(df: pd.DataFrame, op: dict) -> tuple[pd.DataFrame, str, dict]:
    vtype = (op.get("validation_type") or "").strip().lower()
    columns = op.get("columns") or []
    if vtype not in TYPES:
        raise OperationError(
            f"I don't know the validation type '{vtype or '(none)'}' — I can do a list "
            "dropdown, whole/decimal number ranges, date ranges, text length, or a "
            "custom formula rule."
        )
    if not columns:
        raise OperationError("Which column should the validation apply to?")
    for c in columns:
        if c not in df.columns:
            raise OperationError(f"I couldn't find the column '{c}'.")

    allowed = op.get("allowed_values")
    min_v, max_v = op.get("min_value"), op.get("max_value")
    formula = (op.get("formula") or "").strip()
    derived = False

    if vtype == "list":
        if not allowed:
            # Derive the dropdown from the column's own values (first column named).
            s = df[columns[0]].dropna().astype("string").str.strip()
            vals = [v for v in pd.unique(s) if v]
            if not vals:
                raise OperationError(
                    f"'{columns[0]}' has no values to build a dropdown from — tell me "
                    "the allowed options instead."
                )
            if len(vals) > MAX_DERIVED:
                raise OperationError(
                    f"'{columns[0]}' has {len(vals)} distinct values — too many for a "
                    "useful dropdown. Tell me the allowed options explicitly."
                )
            allowed = sorted(vals, key=str.lower)
            derived = True
        allowed = [str(v).strip() for v in allowed if str(v).strip()]
        if not allowed:
            raise OperationError("The dropdown needs at least one allowed value.")
        # Commas inside values break Excel's inline list syntax; the serializer uses a
        # helper-sheet range in that case (and for long lists) — no error needed here.
    elif vtype in ("whole", "decimal", "text_length"):
        if min_v is None and max_v is None:
            raise OperationError(
                f"A {'number' if vtype != 'text_length' else 'text-length'} rule needs "
                "a minimum, a maximum, or both."
            )
        if min_v is not None and max_v is not None and float(min_v) > float(max_v):
            raise OperationError(
                f"The minimum ({min_v}) is larger than the maximum ({max_v}) — swap them?"
            )
    elif vtype == "date":
        if min_v is None and max_v is None:
            raise OperationError("A date rule needs a start date, an end date, or both.")
        for label, v in (("start", min_v), ("end", max_v)):
            if v is not None:
                try:
                    pd.to_datetime(str(v))
                except Exception:
                    raise OperationError(f"I couldn't read '{v}' as the {label} date.")
        if min_v is not None and max_v is not None and pd.to_datetime(str(min_v)) > pd.to_datetime(str(max_v)):
            raise OperationError(f"The start date ({min_v}) is after the end date ({max_v}).")
    elif vtype == "custom" and not formula:
        raise OperationError("A custom rule needs the formula (e.g. {Qty} * {Price} < 100000).")

    # Honesty: how many EXISTING cells already break the rule (per column).
    def violations(col: str) -> int | None:
        s = df[col]
        try:
            if vtype == "list":
                vals = s.dropna().astype("string").str.strip()
                ok = {str(v).strip().lower() for v in allowed}
                return int((~vals.str.lower().isin(ok) & (vals != "")).sum())
            if vtype in ("whole", "decimal"):
                nums = pd.to_numeric(s, errors="coerce")
                mask = pd.Series(False, index=s.index)
                if min_v is not None:
                    mask |= nums < float(min_v)
                if max_v is not None:
                    mask |= nums > float(max_v)
                mask |= nums.isna() & s.notna() & (s.astype("string").str.strip() != "")
                return int(mask.sum())
            if vtype == "date":
                d = pd.to_datetime(s, errors="coerce", format="mixed")
                mask = pd.Series(False, index=s.index)
                if min_v is not None:
                    mask |= d < pd.to_datetime(str(min_v))
                if max_v is not None:
                    mask |= d > pd.to_datetime(str(max_v))
                return int(mask.sum())
            if vtype == "text_length":
                lens = s.astype("string").fillna("").str.len()
                mask = pd.Series(False, index=s.index)
                if min_v is not None:
                    mask |= lens < int(min_v)
                if max_v is not None:
                    mask |= lens > int(max_v)
                return int(mask.sum())
        except Exception:
            return None
        return None

    counts = {c: violations(c) for c in columns}

    directive = {
        "type": "dv", "columns": list(columns), "validation_type": vtype,
        "allowed_values": allowed if vtype == "list" else None,
        "min_value": min_v, "max_value": max_v, "formula": formula or None,
        "input_message": (op.get("input_message") or "").strip() or None,
        "error_message": (op.get("error_message") or "").strip() or None,
        "allow_blank": bool(op.get("allow_blank", True)),
    }

    def _bounds(prefix: str) -> str:
        lo = "" if min_v is None else str(min_v)
        hi = "" if max_v is None else str(max_v)
        rng = f"{lo}–{hi}".strip("–") if lo and hi else (f"at least {lo}" if lo else f"at most {hi}")
        return f"{prefix} {rng}"

    if vtype == "list":
        label = (f"a dropdown of {len(allowed)} option{'s' if len(allowed) != 1 else ''}"
                 + (" (from the column's own values)" if derived else ""))
    elif vtype == "whole":
        label = _bounds("whole numbers")
    elif vtype == "decimal":
        label = _bounds("numbers")
    elif vtype == "date":
        label = _bounds("dates")
    elif vtype == "text_length":
        label = _bounds("text length")
    else:
        label = f"the rule {formula}"
    parts = []
    for c in columns:
        n = counts[c]
        parts.append(f"{c} ({n:,} existing value{'s' if n != 1 else ''} outside the rule)"
                     if n else c)
    note = (f"Added validation — {label} — to {', '.join(parts)}. It checks NEW edits in "
            "Excel; the existing data is unchanged.")
    return df, note, directive
