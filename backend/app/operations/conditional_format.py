"""Engine Phase 1.2 — conditional formatting (catalog Area 5).

The op validates the rule, computes HOW MANY cells currently match (pandas — that's the
honest preview: CF itself only exists in the saved .xlsx), and emits a "cf" directive
the serializer turns into real openpyxl conditional-formatting objects. The data is
never changed — Excel evaluates the rules live from then on.

Rule types (op field "rule_type"):
  greater_than / less_than / between / equal_to / not_equal   value(, value2)
  text_contains                                               value
  date_before / date_after                                    value (ISO date)
  blanks · duplicates · unique
  top_n / bottom_n                                            count (+ percent)
  color_scale (2-3 colors) · data_bars · icon_set (icons: 3/4/5)
  formula                                                     formula ({Col} grammar)
"""
from __future__ import annotations

import pandas as pd

from .base import OperationError

# Excel's classic light-fill/dark-font pairs (the ones its own "Highlight Cells" uses),
# so output looks native, and text stays readable on the tint.
COLORS: dict[str, tuple[str, str]] = {
    "green": ("C6EFCE", "006100"),
    "red": ("FFC7CE", "9C0006"),
    "yellow": ("FFEB9C", "9C6500"),
    "orange": ("FFD8B2", "974706"),
    "blue": ("BDD7EE", "1F4E79"),
    "purple": ("E4DFEC", "403151"),
    "grey": ("D9D9D9", "3F3F3F"),
    "gray": ("D9D9D9", "3F3F3F"),
}

# Solid hexes for data bars / scale stops when the user names a color.
STRONG = {"green": "63BE7B", "red": "F8696B", "yellow": "FFEB84", "orange": "FFB628",
          "blue": "638EC6", "purple": "B1A0C7", "grey": "A6A6A6", "gray": "A6A6A6"}

ICON_SETS = {3: "3TrafficLights1", 4: "4Arrows", 5: "5Rating"}

_COMPARISONS = {"greater_than", "less_than", "between", "equal_to", "not_equal"}
_STANDALONE = {"blanks", "duplicates", "unique", "color_scale", "data_bars"}
RULE_TYPES = _COMPARISONS | _STANDALONE | {
    "text_contains", "date_before", "date_after", "top_n", "bottom_n", "icon_set", "formula",
}


def _matches(df: pd.DataFrame, col: str, rule: str, op: dict, eval_formula) -> int | None:
    """How many cells match the rule RIGHT NOW (None = not countable, e.g. color scale).
    This is the honest preview: the rule itself lives on in Excel."""
    s = df[col]
    nums = pd.to_numeric(s, errors="coerce")
    value, value2 = op.get("value"), op.get("value2")
    try:
        if rule in _COMPARISONS and rule != "between":
            v = float(value)
            return int({"greater_than": nums > v, "less_than": nums < v,
                        "equal_to": nums == v, "not_equal": nums != v}[rule].sum())
        if rule == "between":
            return int(((nums >= float(value)) & (nums <= float(value2))).sum())
        if rule == "text_contains":
            return int(s.astype("string").str.contains(str(value), case=False, na=False).sum())
        if rule in ("date_before", "date_after"):
            d = pd.to_datetime(s, errors="coerce", format="mixed")
            pivot = pd.to_datetime(str(value))
            return int(((d < pivot) if rule == "date_before" else (d > pivot)).sum())
        if rule == "blanks":
            return int((s.isna() | (s.astype("string").str.strip() == "")).sum())
        if rule == "duplicates":
            return int(s[s.notna()].duplicated(keep=False).sum())
        if rule == "unique":
            return int((~s[s.notna()].duplicated(keep=False)).sum())
        if rule in ("top_n", "bottom_n"):
            n = int(op.get("count") or 10)
            if op.get("percent"):
                n = max(1, round(len(nums.dropna()) * n / 100))
            ranked = nums.rank(ascending=rule == "bottom_n", method="min")
            return int((ranked <= n).sum())
        if rule == "formula":
            got = eval_formula(op.get("formula") or "", df)
            return int(pd.Series(got).astype(bool).sum())
    except OperationError:
        raise
    except Exception:
        return None
    return None


def conditional_format(df: pd.DataFrame, op: dict, eval_formula) -> tuple[pd.DataFrame, str, dict]:
    """Validate the rule, count current matches, emit the 'cf' render directive.
    `eval_formula` is the Phase-1.1 evaluator (for formula rules), injected to avoid a
    circular import."""
    rule = (op.get("rule_type") or "").strip().lower()
    columns = op.get("columns") or []
    if rule not in RULE_TYPES:
        raise OperationError(
            f"I don't know the highlight rule '{rule or '(none)'}'. I can do: value "
            "comparisons, text contains, dates, blanks, duplicates/unique, top/bottom N, "
            "color scales, data bars, icon sets, and formula rules."
        )
    if not columns:
        raise OperationError("Which column should the formatting apply to?")
    for c in columns:
        if c not in df.columns:
            raise OperationError(f"I couldn't find the column '{c}'.")

    color = (op.get("color") or "").strip().lower() or None
    if color and color not in COLORS:
        raise OperationError(
            f"I don't have the color '{color}' — try {', '.join(sorted(set(COLORS) - {'gray'}))}."
        )
    if rule in _COMPARISONS and op.get("value") is None:
        raise OperationError(f"The '{rule.replace('_', ' ')}' rule needs a value to compare against.")
    if rule == "between" and op.get("value2") is None:
        raise OperationError("A 'between' rule needs both bounds (value and value2).")
    if rule in ("date_before", "date_after"):
        try:
            pd.to_datetime(str(op.get("value")))
        except Exception:
            raise OperationError(f"I couldn't read '{op.get('value')}' as a date.")
    if rule == "icon_set":
        icons = int(op.get("icons") or 3)
        if icons not in ICON_SETS:
            raise OperationError("Icon sets come in 3, 4, or 5 icons.")
        op = {**op, "icons": icons}
    if rule == "formula" and not (op.get("formula") or "").strip():
        raise OperationError("A formula rule needs the formula (e.g. {Total} > 2 * {Price}).")

    # Honest preview: how many cells match TODAY (the rule stays live in Excel).
    counts: dict[str, int | None] = {c: _matches(df, c, rule, op, eval_formula) for c in columns}

    directive = {
        "type": "cf",
        "columns": list(columns),
        "rule_type": rule,
        "value": op.get("value"),
        "value2": op.get("value2"),
        "color": color,
        "count": op.get("count"),
        "percent": bool(op.get("percent")),
        "icons": op.get("icons"),
        "formula": op.get("formula"),
    }

    pretty = {
        "greater_than": "greater than", "less_than": "less than", "equal_to": "equal to",
        "not_equal": "not equal to", "top_n": "top", "bottom_n": "bottom",
    }
    label = {
        "between": f"between {op.get('value')} and {op.get('value2')}",
        "text_contains": f"containing '{op.get('value')}'",
        "date_before": f"before {op.get('value')}", "date_after": f"after {op.get('value')}",
        "blanks": "blank", "duplicates": "duplicated", "unique": "unique",
        "color_scale": "a color scale", "data_bars": "data bars",
        "icon_set": f"a {op.get('icons') or 3}-icon set", "formula": f"where {op.get('formula')}",
    }.get(rule, f"{pretty.get(rule, rule)} {op.get('value') if rule in _COMPARISONS else op.get('count') or ''}".strip())

    parts = []
    for c in columns:
        n = counts[c]
        parts.append(f"{c} ({n:,} match{'es' if n != 1 else ''} today)" if n is not None else c)
    if rule in ("color_scale", "data_bars", "icon_set"):
        note = f"Applied {label} to {', '.join(columns)}."
    else:
        note = f"Highlighted cells {label} in {', '.join(parts)}" + (f" in {color}." if color else ".")
    note += " The rule stays live in the saved Excel file."
    return df, note, directive
